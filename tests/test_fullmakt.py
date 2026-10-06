"""
Unit tests for official Brønnøysund Fullmakttjenesten API integration, privacy redaction,
authority fact extraction, evidence linkage, update/removal semantics, failure isolation, and CLI/runner metrics.
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
from signal_post.fullmakt import (
    BrregFullmaktClient,
    FULLMAKT_SIGNATUR_SOURCE_ID,
    FULLMAKT_PROKURA_SOURCE_ID,
    FULLMAKT_STATUS_SUCCESS,
    FULLMAKT_STATUS_EMPTY,
    FULLMAKT_STATUS_UNSUPPORTED_ORG_FORM,
    FULLMAKT_STATUS_NOT_FOUND,
    FULLMAKT_STATUS_FAILED,
    FULLMAKT_STATUS_MALFORMED,
    extract_fullmakt_facts_and_evidence,
    redact_person_privacy,
    save_fullmakt_facts_to_storage,
)
from signal_post.models import NormalizedCompanyProfile
from signal_post.runner import CompanyRunner, run_evaluation
from signal_post.storage import get_connection, get_facts, init_db, save_company_profile
from signal_post.cli import main as cli_main


class TestFullmaktIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_fullmakt.db")
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

    def test_privacy_redaction_sensitive_keys(self):
        """Verify redact_person_privacy strips birth dates, national ID numbers, and D-numbers."""
        sample_payload = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "signeringsGrunnlag": {
                "signaturProkuraFritekst": "Styret i fellesskap",
                "muligeSigneringsRoller": [
                    {
                        "personRolleGrunnlag": {
                            "navn": "Ola Nordmann",
                            "fodselsdato": "01.01.1980",
                            "fødselsdato": "01.01.1980",
                            "fodselsnummer": "01018012345",
                            "fnr": "01018012345",
                            "d-number": "123456",
                            "d_number": "123456",
                            "rolle": {"kode": "KONT", "beskrivelse": "Styrets leder"},
                        }
                    }
                ],
            },
        }

        sanitized = redact_person_privacy(sample_payload)
        sanitized_str = json.dumps(sanitized)

        self.assertNotIn("01.01.1980", sanitized_str)
        self.assertNotIn("01018012345", sanitized_str)
        self.assertNotIn("fodselsdato", sanitized_str)
        self.assertNotIn("fødselsdato", sanitized_str)
        self.assertNotIn("fnr", sanitized_str)
        self.assertNotIn("d-number", sanitized_str)

        # Confirm legitimate name and role remain
        self.assertIn("Ola Nordmann", sanitized_str)
        self.assertIn("Styret i fellesskap", sanitized_str)

    def test_successful_signatur_fetch(self):
        """Test successful signature fetch and classification."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "status": {"rutineStatus": {"kode": "OK"}},
            "signeringsGrunnlag": {
                "signaturProkuraFritekst": "Styret i fellesskap",
            },
        }

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_SUCCESS)
        self.assertEqual(res.fullmakt_type, "signatur")
        self.assertEqual(res.http_status, 200)
        self.assertIsNotNone(res.payload)

    def test_successful_prokura_fetch(self):
        """Test successful prokura fetch."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "status": {"rutineStatus": {"kode": "OK"}},
            "signeringsGrunnlag": {
                "signaturProkuraFritekst": "Prokura i fellesskap med daglig leder",
            },
        }

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_prokura("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_SUCCESS)
        self.assertEqual(res.fullmakt_type, "prokura")
        self.assertEqual(res.http_status, 200)

    def test_exact_company_identity_rejection(self):
        """Reject response when enhet organisasjonsnummer mismatches requested org number."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "enhet": {"organisasjonsnummer": "999999999"},  # Mismatched org
            "status": {"rutineStatus": {"kode": "OK"}},
        }

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_FAILED)
        self.assertIn("Identity mismatch", res.error)

    def test_unsupported_organization_form(self):
        """Classify rutineStatus.kode == 'NA' as unsupported_org_form."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "status": {"rutineStatus": {"kode": "NA", "beskrivelse": "Ikke støttet"}},
        }

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_UNSUPPORTED_ORG_FORM)
        self.assertTrue(res.is_complete_set)

    def test_no_data_empty_behavior(self):
        """Classify response with rutineStatus OK but zero rules as success_empty."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "status": {"rutineStatus": {"kode": "OK"}},
            "signeringsGrunnlag": {},
            "signeringsKombinasjon": {},
        }

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_EMPTY)
        self.assertTrue(res.is_complete_set)

    def test_http_404_not_found(self):
        """Classify HTTP 404 cleanly as not_found."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_NOT_FOUND)
        self.assertEqual(res.http_status, 404)

    def test_malformed_json(self):
        """Classify malformed JSON response as malformed."""
        client = BrregFullmaktClient()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.side_effect = ValueError("Bad JSON")

        with patch.object(client.session, "get", return_value=mock_response):
            res = client.fetch_signatur("923609016")

        self.assertEqual(res.status, FULLMAKT_STATUS_MALFORMED)

    def test_budget_exhaustion(self):
        """Raise RequestBudgetExceededError when budget is exhausted."""
        tracker = MagicMock(spec=RequestBudgetTracker)
        tracker.can_make_request.return_value = False

        client = BrregFullmaktClient(budget_tracker=tracker)

        with self.assertRaises(RequestBudgetExceededError):
            client.fetch_signatur("923609016")

    def test_fact_extraction_and_storage(self):
        """Extract and store signature facts into SQLite with evidence linkage."""
        payload = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "signeringsGrunnlag": {
                "signaturProkuraFritekst": "Styrets leder og ett styremedlem i fellesskap",
                "muligeSigneringsRoller": [
                    {
                        "personRolleGrunnlag": {
                            "navn": "Kari Nordmann",
                            "fodselsdato": "15.05.1985",
                            "rolle": {"kode": "LEDE", "beskrivelse": "Styrets leder"},
                        }
                    }
                ],
            },
            "signeringsKombinasjon": {
                "kombinasjon": [
                    {
                        "kombinasjonsId": 1,
                        "kode": "KOMB_1",
                        "tekstforklaring": "Styrets leder i fellesskap med daglig leder",
                    }
                ]
            },
        }

        extracted = extract_fullmakt_facts_and_evidence(
            payload, "923609016", "signatur", "https://data.brreg.no/fullmakt/enheter/923609016/signatur"
        )
        self.assertGreater(len(extracted), 0)

        counts = save_fullmakt_facts_to_storage(self.conn, "923609016", "signatur", extracted)
        self.assertGreater(counts["created"], 0)

        # Inspect SQLite facts
        facts = get_facts(self.conn, "923609016")
        fullmakt_facts = [f for f in facts if f["fact_key"].startswith("fullmakt_signatur_")]
        self.assertGreater(len(fullmakt_facts), 0)

        # Confirm zero birth dates in SQLite facts or raw evidence
        cur = self.conn.execute("SELECT raw_evidence FROM evidence WHERE source_id = ?;", (FULLMAKT_SIGNATUR_SOURCE_ID,))
        ev_rows = cur.fetchall()
        for row in ev_rows:
            self.assertNotIn("15.05.1985", row[0])

    def test_removal_semantics(self):
        """Verify fact absent from complete current response is deactivated."""
        payload = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "signeringsGrunnlag": {
                "signaturProkuraFritekst": "Initial Rule",
            },
        }

        extracted = extract_fullmakt_facts_and_evidence(
            payload, "923609016", "signatur", "https://data.brreg.no/fullmakt/enheter/923609016/signatur"
        )
        save_fullmakt_facts_to_storage(self.conn, "923609016", "signatur", extracted, complete_set=True)

        # Re-save with empty facts
        counts = save_fullmakt_facts_to_storage(self.conn, "923609016", "signatur", [], complete_set=True)
        self.assertEqual(counts["removed"], 1)

        # Active facts should now be empty
        facts = get_facts(self.conn, "923609016", active_only=True)
        fullmakt_facts = [f for f in facts if f["fact_key"].startswith("fullmakt_signatur_")]
        self.assertEqual(len(fullmakt_facts), 0)

    def test_runner_integration_sources_option(self):
        """Test CompanyRunner execution with include_fullmakt=True."""
        runner = CompanyRunner(self.conn)

        mock_sig_res = MagicMock()
        mock_sig_res.status = FULLMAKT_STATUS_SUCCESS
        mock_sig_res.is_complete_set = True
        mock_sig_res.payload = {
            "enhet": {"organisasjonsnummer": "923609016"},
            "signeringsGrunnlag": {"signaturProkuraFritekst": "Styret i fellesskap"},
        }
        mock_sig_res.source_url = "https://data.brreg.no/fullmakt/enheter/923609016/signatur"
        mock_sig_res.retrieved_at = "2026-10-06T00:00:00Z"
        mock_sig_res.error = None

        mock_prok_res = MagicMock()
        mock_prok_res.status = FULLMAKT_STATUS_EMPTY
        mock_prok_res.is_complete_set = True
        mock_prok_res.payload = None
        mock_prok_res.source_url = "https://data.brreg.no/fullmakt/enheter/923609016/prokura"
        mock_prok_res.retrieved_at = "2026-10-06T00:00:00Z"
        mock_prok_res.error = None

        with patch.object(runner.fullmakt_client, "fetch_signatur", return_value=mock_sig_res), \
             patch.object(runner.fullmakt_client, "fetch_prokura", return_value=mock_prok_res):
            res = runner.process_company("923609016", live_refresh=True, include_fullmakt=True)

        self.assertEqual(res["source_status"]["signatur"], FULLMAKT_STATUS_SUCCESS)
        self.assertEqual(res["source_status"]["prokura"], FULLMAKT_STATUS_EMPTY)


if __name__ == "__main__":
    unittest.main()
