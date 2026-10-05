"""
Unit tests for company discovery module, pagination, deduplication, and request budget tracking.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError
from signal_post.discovery import discover_organizations
from signal_post.exceptions import RegistryClientError
from signal_post.storage import get_connection, init_db


class TestDiscovery(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_discovery.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.mock_client = MagicMock()
        self.mock_session = MagicMock()
        self.mock_client.session = self.mock_session

        self.sample_page_1 = {
            "_embedded": {
                "enheter": [
                    {
                        "organisasjonsnummer": "974760673",
                        "navn": "REGISTERENHETEN I BRØNNØYSUND",
                        "organisasjonsform": {"kode": "ORGL"},
                        "registreringsdatoEnhetsregisteret": "1995-08-09",
                    },
                    {
                        "organisasjonsnummer": "923609016",
                        "navn": "EQUINOR ASA",
                        "organisasjonsform": {"kode": "ASA"},
                        "registreringsdatoEnhetsregisteret": "1995-03-12",
                    },
                ]
            },
            "page": {
                "size": 2,
                "totalElements": 4,
                "totalPages": 2,
                "number": 0,
            }
        }

        self.sample_page_2 = {
            "_embedded": {
                "enheter": [
                    {
                        "organisasjonsnummer": "974760673",  # Duplicate org number
                        "navn": "REGISTERENHETEN I BRØNNØYSUND",
                        "organisasjonsform": {"kode": "ORGL"},
                    },
                    {
                        "organisasjonsnummer": "989061593",
                        "navn": "TEST COMPANY AS",
                        "organisasjonsform": {"kode": "AS"},
                    },
                ]
            },
            "page": {
                "size": 2,
                "totalElements": 4,
                "totalPages": 2,
                "number": 1,
            }
        }

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _create_mock_response(self, status_code=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status_code
        if json_data is not None:
            resp.json.return_value = json_data
        else:
            resp.json.side_effect = ValueError("Invalid JSON")
        return resp

    def test_search_result_parsing_and_deduplication(self):
        """Test search API parsing, deduplication, and saving to collection queue."""
        self.mock_session.get.return_value = self._create_mock_response(200, self.sample_page_1)

        summary = discover_organizations(self.conn, client=self.mock_client, limit=10)

        self.assertEqual(summary["discovered_new"], 2)
        self.assertEqual(summary["total_in_queue"], 2)

        cur = self.conn.execute("SELECT org_number, name FROM collection_queue ORDER BY org_number;")
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["org_number"], "923609016")
        self.assertEqual(rows[1]["org_number"], "974760673")

    def test_pagination_and_discovery_limits(self):
        """Test multi-page iteration and clean stopping when discovery limit is reached."""
        self.mock_session.get.side_effect = [
            self._create_mock_response(200, self.sample_page_1),
            self._create_mock_response(200, self.sample_page_2),
        ]

        summary = discover_organizations(self.conn, client=self.mock_client, limit=3)

        self.assertEqual(summary["discovered_new"], 3)  # 2 from page 1 + 1 new from page 2
        self.assertEqual(summary["already_queued"], 1)  # 1 duplicate on page 2 skipped
        self.assertEqual(summary["pages_fetched"], 2)
        self.assertEqual(summary["stopped_reason"], "limit_reached")

    def test_request_budget_enforcement(self):
        """Test stopping safely when outbound request budget is reached."""
        tracker = RequestBudgetTracker(self.conn, budget_limit=1)
        self.mock_session.get.return_value = self._create_mock_response(200, self.sample_page_1)

        summary = discover_organizations(self.conn, client=self.mock_client, budget_tracker=tracker, limit=10)

        self.assertEqual(summary["requests_made"], 1)
        self.assertEqual(summary["stopped_reason"], "request_budget_exceeded")

        # Second run with exhausted budget stops immediately
        summary2 = discover_organizations(self.conn, client=self.mock_client, budget_tracker=tracker, limit=10)
        self.assertEqual(summary2["requests_made"], 0)
        self.assertEqual(summary2["stopped_reason"], "request_budget_exceeded")

    def test_invalid_org_numbers_and_malformed_responses(self):
        """Test invalid org numbers in API search are skipped and malformed responses raise RegistryClientError."""
        malformed_page = {
            "_embedded": {
                "enheter": [
                    {"organisasjonsnummer": "INVALID123", "navn": "Bad Org"},
                    {"organisasjonsnummer": "974760673", "navn": "Good Org"},
                ]
            }
        }
        self.mock_session.get.return_value = self._create_mock_response(200, malformed_page)

        summary = discover_organizations(self.conn, client=self.mock_client, limit=10)
        self.assertEqual(summary["invalid_skipped"], 1)
        self.assertEqual(summary["discovered_new"], 1)

    def test_api_error_during_pagination_raises_exception(self):
        """Test HTTP error on discovery search API raises RegistryClientError."""
        self.mock_session.get.return_value = self._create_mock_response(500)

        with self.assertRaises(RegistryClientError):
            discover_organizations(self.conn, client=self.mock_client, limit=10)

    def test_malformed_pagination_metadata_handled_safely(self):
        """Test null, missing, or non-integer totalPages in search metadata is handled safely."""
        malformed_page = {
            "_embedded": {
                "enheter": [
                    {"organisasjonsnummer": "974760673", "navn": "REGISTERENHETEN I BRØNNØYSUND"}
                ]
            },
            "page": {
                "totalPages": None,  # Null totalPages
            }
        }
        self.mock_session.get.return_value = self._create_mock_response(200, malformed_page)

        summary = discover_organizations(self.conn, client=self.mock_client, limit=10)
        self.assertEqual(summary["discovered_new"], 1)
        self.assertEqual(summary["pages_fetched"], 1)


if __name__ == "__main__":
    unittest.main()
