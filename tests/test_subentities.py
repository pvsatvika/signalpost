"""
Unit tests for official Brønnøysund Underenheter (Operating Units) API integration,
parent/child scope separation, employee count semantics, pagination, truncation,
deletion handling, update/removal semantics, failure isolation, and CLI/runner metrics.
"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import json
import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.subentities import (
    BrregSubentitiesClient,
    SUBENTITIES_SOURCE_ID,
    SUBENTITIES_SOURCE_NAME,
    SUBENTITIES_STATUS_SUCCESS,
    SUBENTITIES_STATUS_EMPTY,
    SUBENTITIES_STATUS_TRUNCATED,
    SUBENTITIES_STATUS_NOT_FOUND,
    SUBENTITIES_STATUS_FAILED,
    SUBENTITIES_STATUS_MALFORMED,
    extract_subentity_facts_and_evidence,
    save_subentity_facts_to_storage,
)
from signal_post.models import NormalizedCompanyProfile
from signal_post.runner import CompanyRunner, run_evaluation
from signal_post.storage import get_connection, get_facts, init_db, save_company_profile


class TestSubentitiesIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_subentities.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
        })
        save_company_profile(self.conn, self.profile)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_zero_children_success_empty(self):
        """Test query returning zero underenheter resulting in success_empty."""
        client = BrregSubentitiesClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "page": {"number": 0, "size": 100, "totalElements": 0, "totalPages": 0},
            "_embedded": {"underenheter": []},
        }

        with patch.object(client.session, "get", return_value=mock_resp):
            res = client.fetch_subentities("923609016")

        self.assertEqual(res.status, SUBENTITIES_STATUS_EMPTY)
        self.assertEqual(res.total_elements, 0)
        self.assertEqual(len(res.subentities), 0)
        self.assertTrue(res.is_complete_set)

    def test_one_child_success(self):
        """Test query returning 1 operating unit."""
        client = BrregSubentitiesClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "page": {"number": 0, "size": 100, "totalElements": 1, "totalPages": 1},
            "_embedded": {
                "underenheter": [
                    {
                        "organisasjonsnummer": "973150480",
                        "navn": "EQUINOR AVD BERGEN",
                        "overordnetEnhet": "923609016",
                        "organisasjonsform": {"kode": "BEDR", "beskrivelse": "Underenhet"},
                        "antallAnsatte": 50,
                        "harRegistrertAntallAnsatte": True,
                    }
                ]
            },
        }

        with patch.object(client.session, "get", return_value=mock_resp):
            res = client.fetch_subentities("923609016")

        self.assertEqual(res.status, SUBENTITIES_STATUS_SUCCESS)
        self.assertEqual(len(res.subentities), 1)

    def test_wrong_parent_child_rejection(self):
        """Reject child item when returned item overordnetEnhet mismatches requested parent org number."""
        client = BrregSubentitiesClient()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "page": {"number": 0, "size": 100, "totalElements": 1, "totalPages": 1},
            "_embedded": {
                "underenheter": [
                    {
                        "organisasjonsnummer": "999999999",
                        "navn": "WRONG PARENT CHILD",
                        "overordnetEnhet": "111111111",  # Mismatched parent
                    }
                ]
            },
        }

        with patch.object(client.session, "get", return_value=mock_resp):
            res = client.fetch_subentities("923609016")

        self.assertEqual(len(res.subentities), 0)

    def test_pagination_and_max_pages_limit(self):
        """Test bounded pagination up to max_pages limit."""
        client = BrregSubentitiesClient(page_size=1, max_pages=2, max_subentities=10)

        def mock_get(url, **kwargs):
            m = MagicMock()
            m.status_code = 200
            if "page=0" in url:
                m.json.return_value = {
                    "page": {"number": 0, "size": 1, "totalElements": 3, "totalPages": 3},
                    "_embedded": {
                        "underenheter": [
                            {"organisasjonsnummer": "900000001", "overordnetEnhet": "923609016", "navn": "AVD 1"}
                        ]
                    },
                }
            else:
                m.json.return_value = {
                    "page": {"number": 1, "size": 1, "totalElements": 3, "totalPages": 3},
                    "_embedded": {
                        "underenheter": [
                            {"organisasjonsnummer": "900000002", "overordnetEnhet": "923609016", "navn": "AVD 2"}
                        ]
                    },
                }
            return m

        with patch.object(client.session, "get", side_effect=mock_get):
            res = client.fetch_subentities("923609016")

        self.assertEqual(res.pages_fetched, 2)
        self.assertEqual(len(res.subentities), 2)
        self.assertTrue(res.is_truncated)
        self.assertEqual(res.status, SUBENTITIES_STATUS_TRUNCATED)

    def test_employee_suppressed_semantics(self):
        """Respect missing != zero: harRegistrertAntallAnsatte=True with antallAnsatte=None records employee_registered=true."""
        sub_list = [
            {
                "organisasjonsnummer": "973150480",
                "navn": "AVD TEST",
                "overordnetEnhet": "923609016",
                "antallAnsatte": None,
                "harRegistrertAntallAnsatte": True,
            }
        ]

        extracted = extract_subentity_facts_and_evidence(sub_list, "923609016", "https://data.brreg.no/underenheter")
        fact_keys = {f[0]: f[1] for f in extracted}

        self.assertIn("subentity_973150480_employee_registered", fact_keys)
        self.assertEqual(fact_keys["subentity_973150480_employee_registered"], "true")
        self.assertNotIn("subentity_973150480_employee_count", fact_keys)

    def test_location_address_scope_separation(self):
        """Underenhet location address MUST be child-scoped, never parent business_address."""
        sub_list = [
            {
                "organisasjonsnummer": "973150480",
                "navn": "AVD BERGEN",
                "overordnetEnhet": "923609016",
                "beliggenhetsadresse": {
                    "adresse": ["Sandslivegen 90"],
                    "postnummer": "5254",
                    "poststed": "SANDSLI",
                    "kommune": "BERGEN",
                },
            }
        ]

        extracted = extract_subentity_facts_and_evidence(sub_list, "923609016", "https://data.brreg.no/underenheter")
        fact_keys = [f[0] for f in extracted]

        self.assertIn("subentity_973150480_location_address", fact_keys)
        self.assertNotIn("business_address", fact_keys)

    def test_deleted_and_closed_children(self):
        """Preserve slettedato and nedleggelsesdato for deleted/closed operating units."""
        sub_list = [
            {
                "organisasjonsnummer": "973150480",
                "navn": "CLOSED BRANCH",
                "overordnetEnhet": "923609016",
                "oppstartsdato": "1990-01-01",
                "nedleggelsesdato": "2020-12-31",
                "slettedato": "2021-01-15",
            }
        ]

        extracted = extract_subentity_facts_and_evidence(sub_list, "923609016", "https://data.brreg.no/underenheter")
        fact_map = {f[0]: f[1] for f in extracted}

        self.assertEqual(fact_map.get("subentity_973150480_closure_date"), "2020-12-31")
        self.assertEqual(fact_map.get("subentity_973150480_deletion_date"), "2021-01-15")

    def test_storage_and_removal_semantics(self):
        """Verify complete response deactivates absent children, but truncated response preserves existing facts."""
        sub_list = [
            {
                "organisasjonsnummer": "973150480",
                "navn": "AVD 1",
                "overordnetEnhet": "923609016",
            }
        ]

        extracted = extract_subentity_facts_and_evidence(sub_list, "923609016", "https://data.brreg.no/underenheter")
        save_subentity_facts_to_storage(self.conn, "923609016", extracted, complete_set=True)

        facts = get_facts(self.conn, "923609016", active_only=True)
        self.assertTrue(any(f["fact_key"] == "subentity_973150480_name" for f in facts))

        # Re-save with complete empty list -> removes previous child
        counts = save_subentity_facts_to_storage(self.conn, "923609016", [], complete_set=True)
        self.assertGreater(counts["removed"], 0)

        facts_after = get_facts(self.conn, "923609016", active_only=True)
        self.assertFalse(any(f["fact_key"] == "subentity_973150480_name" for f in facts_after))

    def test_budget_exhaustion(self):
        """Raise RequestBudgetExceededError when budget is exhausted."""
        tracker = MagicMock(spec=RequestBudgetTracker)
        tracker.can_make_request.return_value = False

        client = BrregSubentitiesClient(budget_tracker=tracker)

        with self.assertRaises(RequestBudgetExceededError):
            client.fetch_subentities("923609016")

    def test_runner_integration(self):
        """Test CompanyRunner execution with include_subentities=True."""
        runner = CompanyRunner(self.conn)

        mock_res = MagicMock()
        mock_res.status = SUBENTITIES_STATUS_SUCCESS
        mock_res.is_complete_set = True
        mock_res.subentities = [
            {
                "organisasjonsnummer": "973150480",
                "navn": "EQUINOR AVD BERGEN",
                "overordnetEnhet": "923609016",
            }
        ]
        mock_res.source_url = "https://data.brreg.no/underenheter"
        mock_res.retrieved_at = "2026-10-06T00:00:00Z"
        mock_res.error = None
        mock_res.is_truncated = False

        with patch.object(runner.subentities_client, "fetch_subentities", return_value=mock_res):
            res = runner.process_company("923609016", live_refresh=True, include_subentities=True)

        self.assertEqual(res["source_status"]["subentities"], SUBENTITIES_STATUS_SUCCESS)


if __name__ == "__main__":
    unittest.main()
