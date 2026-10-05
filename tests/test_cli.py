"""
Unit tests for CLI entry point.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
import io
import json
import sys

from signal_post.cli import main
from signal_post.exceptions import InvalidOrgNumberError, CompanyNotFoundError
from signal_post.models import NormalizedCompanyProfile


class TestCLI(unittest.TestCase):

    @patch("signal_post.cli.BrregClient")
    def test_cli_human_output_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND"
        })
        mock_client.get_company_profile.return_value = mock_profile

        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            main(["974760673"])

        output = stdout.getvalue()
        self.assertIn("REGISTERENHETEN I BRØNNØYSUND", output)
        self.assertIn("974760673", output)
        mock_client.get_company_profile.assert_called_once_with("974760673")

    @patch("signal_post.cli.BrregClient")
    def test_cli_json_output_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND"
        })
        mock_client.get_company_profile.return_value = mock_profile

        stdout = io.StringIO()
        with patch.object(sys, "stdout", stdout):
            main(["974760673", "--json"])

        output = stdout.getvalue()
        parsed = json.loads(output)
        self.assertEqual(parsed["org_number"], "974760673")
        self.assertEqual(parsed["name"], "REGISTERENHETEN I BRØNNØYSUND")
        self.assertNotIn("raw_data", parsed)

    @patch("signal_post.cli.BrregClient")
    def test_cli_error_exit(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_client.get_company_profile.side_effect = InvalidOrgNumberError("Invalid format")

        stderr = io.StringIO()
        with patch.object(sys, "stderr", stderr):
            with self.assertRaises(SystemExit) as ctx:
                main(["123"])
            self.assertEqual(ctx.exception.code, 1)

        err_output = stderr.getvalue()
        self.assertIn("Error: Invalid format", err_output)


    @patch("signal_post.cli.BrregClient")
    @patch("signal_post.cli.discover_organizations")
    @patch("signal_post.cli.collect_queued_profiles")
    def test_cli_small_run_sequence_offline(self, mock_collect, mock_discover, mock_client_cls):
        """Verify CLI supports the small-run sequence (--discover, --status, --collect, --status) cleanly."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            db_path = str(Path(temp_dir.name) / "signalpost_test.db")

            mock_discover.return_value = {
                "discovered_new": 5, "already_queued": 0, "total_in_queue": 5,
                "pending_in_queue": 5, "pages_fetched": 1, "requests_made": 1,
                "stopped_reason": "limit_reached"
            }
            mock_collect.return_value = {
                "attempted": 5, "completed": 5, "failed": 0, "requests_made": 5,
                "stopped_reason": "completed", "total_queued": 5, "pending_in_queue": 0,
                "completed_in_queue": 5, "failed_in_queue": 0, "stored_companies_count": 5,
                "requests_today": 6, "remaining_budget": 4
            }

            # 1. Discover
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                main(["--discover", "--limit", "5", "--filter-org-form", "AS", "--db", db_path])
            self.assertIn("DISCOVERY WORKFLOW COMPLETED", stdout.getvalue())

            # 2. Status
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                main(["--status", "--db", db_path])
            self.assertIn("SIGNALPOST DISCOVERY & COLLECTION STATUS", stdout.getvalue())

            # 3. Collect
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                main(["--collect", "--limit", "5", "--request-budget", "10", "--db", db_path])
            self.assertIn("COLLECTION WORKFLOW COMPLETED", stdout.getvalue())

            # 4. Status again
            stdout = io.StringIO()
            with patch.object(sys, "stdout", stdout):
                main(["--status", "--db", db_path])
            self.assertIn("SIGNALPOST DISCOVERY & COLLECTION STATUS", stdout.getvalue())
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
