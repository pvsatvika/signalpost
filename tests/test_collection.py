"""
Unit tests for resumable collection workflow, queue management, progress tracking, and budget enforcement.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from signal_post.budget import RequestBudgetTracker
from signal_post.collection import collect_queued_profiles, get_collection_status
from signal_post.discovery import discover_organizations
from signal_post.exceptions import RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.storage import get_connection, init_db, get_company, get_facts


class TestCollection(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_collection.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.mock_client = MagicMock()

        self.raw_1 = {
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 487,
        }
        self.profile_1 = NormalizedCompanyProfile.from_raw_dict(self.raw_1)

        self.raw_2 = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "antallAnsatte": 21272,
        }
        self.profile_2 = NormalizedCompanyProfile.from_raw_dict(self.raw_2)

        # Pre-seed queue with 2 pending orgs
        with self.conn:
            self.conn.execute("""
                INSERT INTO collection_queue (org_number, name, discovered_at, discovery_source, status)
                VALUES ('974760673', 'REGISTERENHETEN I BRØNNØYSUND', '2026-10-04T10:00:00Z', 'brreg', 'pending'),
                       ('923609016', 'EQUINOR ASA', '2026-10-04T10:01:00Z', 'brreg', 'pending');
            """)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_collect_queued_profiles_success(self):
        """Test processing queued org numbers collects profiles and marks status 'completed'."""
        self.mock_client.get_company_profile.side_effect = [self.profile_1, self.profile_2]

        summary = collect_queued_profiles(self.conn, client=self.mock_client, limit=10)

        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["pending_in_queue"], 0)

        # Check DB status
        comp1 = get_company(self.conn, "974760673")
        comp2 = get_company(self.conn, "923609016")
        self.assertIsNotNone(comp1)
        self.assertIsNotNone(comp2)

    def test_resume_interrupted_or_failed_collection(self):
        """Test resuming collection skips completed items and processes pending/failed items."""
        # First run: item 1 succeeds, item 2 fails due to network error
        self.mock_client.get_company_profile.side_effect = [
            self.profile_1,
            RegistryClientError("Network error"),
        ]

        summary1 = collect_queued_profiles(self.conn, client=self.mock_client, limit=10)
        self.assertEqual(summary1["completed"], 1)
        self.assertEqual(summary1["failed"], 1)

        # Second run: standard collect skips completed & failed items (only processing pending)
        summary2 = collect_queued_profiles(self.conn, client=self.mock_client, limit=10, resume_failed=False)
        self.assertEqual(summary2["attempted"], 0)

        # Resume run: resume_failed=True retries failed item 2 and completes it
        self.mock_client.get_company_profile.side_effect = [self.profile_2]
        summary3 = collect_queued_profiles(self.conn, client=self.mock_client, limit=10, resume_failed=True)

        self.assertEqual(summary3["attempted"], 1)
        self.assertEqual(summary3["completed"], 1)
        self.assertEqual(summary3["failed_in_queue"], 0)

    def test_collection_request_budget_enforcement(self):
        """Test collection halts processing when request budget is exhausted."""
        tracker = RequestBudgetTracker(self.conn, budget_limit=1)
        self.mock_client.get_company_profile.return_value = self.profile_1

        summary = collect_queued_profiles(self.conn, client=self.mock_client, budget_tracker=tracker, limit=10)

        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["requests_made"], 1)
        self.assertEqual(summary["stopped_reason"], "request_budget_exceeded")
        self.assertEqual(summary["pending_in_queue"], 1)

    def test_failed_collection_preserves_existing_stored_profile(self):
        """Test that a failed collection attempt does NOT overwrite or corrupt previously stored profile data."""
        # Pre-seed profile 1 in DB
        self.mock_client.get_company_profile.return_value = self.profile_1
        collect_queued_profiles(self.conn, client=self.mock_client, limit=1)

        # Reset queue status for item 1 to pending
        with self.conn:
            self.conn.execute("UPDATE collection_queue SET status = 'pending' WHERE org_number = '974760673';")

        # Second attempt fails
        self.mock_client.get_company_profile.side_effect = RegistryClientError("500 Server Error")
        collect_queued_profiles(self.conn, client=self.mock_client, limit=1)

        # Profile in DB must remain valid and intact
        comp = get_company(self.conn, "974760673")
        self.assertEqual(comp["name"], "REGISTERENHETEN I BRØNNØYSUND")

    def test_get_collection_status_summary(self):
        """Test get_collection_status metrics aggregation."""
        status = get_collection_status(self.conn)
        self.assertEqual(status["total_queued"], 2)
        self.assertEqual(status["pending_count"], 2)
        self.assertEqual(status["completed_count"], 0)

    def test_collection_unhandled_exception_handling(self):
        """Test that an unhandled exception during item refresh marks item failed and continues batch."""
        self.mock_client.get_company_profile.side_effect = RuntimeError("Unexpected internal crash")

        summary = collect_queued_profiles(self.conn, client=self.mock_client, limit=1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["failed"], 1)

        cur = self.conn.execute("SELECT status, error_message, attempt_count FROM collection_queue WHERE org_number = '974760673';")
        row = cur.fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("Unexpected internal crash", row["error_message"])
        self.assertEqual(row["attempt_count"], 1)


if __name__ == "__main__":
    unittest.main()
