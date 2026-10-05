"""
Unit tests for safe profile refresh workflow, change detection, and structured change explanations.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from signal_post.exceptions import DataMismatchError, InvalidOrgNumberError, RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.refresh import refresh_company, RefreshResult, FactChangeItem
from signal_post.storage import get_connection, init_db, get_company, get_facts, get_change_history, get_evidence_for_fact


class TestRefresh(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_refresh.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.mock_client = MagicMock()

        self.sample_raw_1 = {
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 487,
            "hjemmeside": "www.brreg.no",
            "organisasjonsform": {"kode": "ORGL", "beskrivelse": "Organisasjonsledd"},
            "registreringsdatoEnhetsregisteret": "1995-08-09",
        }
        self.profile_1 = NormalizedCompanyProfile.from_raw_dict(self.sample_raw_1)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_first_time_profile_refresh(self):
        """Test first-time refresh inserts company, active facts, evidence, and creates 'created' change report."""
        self.mock_client.get_company_profile.return_value = self.profile_1

        result = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result.status, "success")
        self.assertEqual(result.org_number, "974760673")
        self.assertEqual(result.company_name, "REGISTERENHETEN I BRØNNØYSUND")
        self.assertTrue(result.summary_counts["created"] > 0)

        # Check explanation format
        created_item = [c for c in result.changes if c.change_type == "created"][0]
        self.assertIn("was observed for the first time", created_item.explanation)

        # Check DB content
        comp = get_company(self.conn, "974760673")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["name"], "REGISTERENHETEN I BRØNNØYSUND")

    def test_unchanged_repeated_refresh(self):
        """Test repeated refresh with identical data updates timestamps but logs 'unchanged' in report and no extra change history."""
        self.mock_client.get_company_profile.return_value = self.profile_1

        # First refresh
        refresh_company(self.conn, "974760673", client=self.mock_client)
        initial_history_len = len(get_change_history(self.conn, "974760673"))

        # Second refresh
        result2 = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result2.status, "success")
        self.assertEqual(result2.summary_counts["created"], 0)
        self.assertTrue(result2.summary_counts["unchanged"] > 0)

        # History count must not increase
        second_history_len = len(get_change_history(self.conn, "974760673"))
        self.assertEqual(second_history_len, initial_history_len)

        # Evidence records should be appended
        facts = get_facts(self.conn, "974760673", active_only=True)
        name_fact = [f for f in facts if f["fact_key"] == "name"][0]
        evidence = get_evidence_for_fact(self.conn, name_fact["fact_id"])
        self.assertEqual(len(evidence), 2)

    def test_genuine_same_source_value_change(self):
        """Test genuine value change from same source records previous and new values with explanation."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        refresh_company(self.conn, "974760673", client=self.mock_client)

        # Updated profile with new employee count
        updated_raw = dict(self.sample_raw_1)
        updated_raw["antallAnsatte"] = 510
        profile_updated = NormalizedCompanyProfile.from_raw_dict(updated_raw)
        self.mock_client.get_company_profile.return_value = profile_updated

        result2 = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result2.status, "success")
        self.assertEqual(result2.summary_counts["updated"], 1)

        emp_change = [c for c in result2.changes if c.fact_key == "num_employees"][0]
        self.assertEqual(emp_change.change_type, "updated")
        self.assertEqual(emp_change.previous_value, "487")
        self.assertEqual(emp_change.new_value, "510")
        self.assertIn("changed from '487' to '510'", emp_change.explanation)

    def test_missing_field_in_later_response(self):
        """Test field missing in later API response is reported as 'omitted' and NOT deleted from database."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        refresh_company(self.conn, "974760673", client=self.mock_client)

        # Profile with omitted website
        omitted_raw = dict(self.sample_raw_1)
        omitted_raw.pop("hjemmeside", None)
        profile_omitted = NormalizedCompanyProfile.from_raw_dict(omitted_raw)
        self.mock_client.get_company_profile.return_value = profile_omitted

        result2 = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result2.status, "success")
        self.assertEqual(result2.summary_counts["omitted"], 1)

        website_item = [c for c in result2.changes if c.fact_key == "website"][0]
        self.assertEqual(website_item.change_type, "omitted")
        self.assertEqual(website_item.previous_value, "www.brreg.no")
        self.assertIsNone(website_item.new_value)
        self.assertIn("was omitted in the latest response", website_item.explanation)

        # Active website fact MUST remain stored in database
        active_facts = get_facts(self.conn, "974760673", active_only=True)
        website_fact = [f for f in active_facts if f["fact_key"] == "website"][0]
        self.assertEqual(website_fact["fact_value"], "www.brreg.no")

    def test_invalid_org_number_identity_mismatch(self):
        """Test identity mismatch or invalid org number stops refresh before database modifications."""
        self.mock_client.get_company_profile.side_effect = DataMismatchError("Org number mismatch")

        result = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result.status, "failed")
        self.assertIn("mismatch", result.error_message)

        # Database must have no company record
        comp = get_company(self.conn, "974760673")
        self.assertIsNone(comp)

    def test_api_timeout_or_failure(self):
        """Test API timeout returns failed result without corrupting stored profile."""
        # Pre-seed DB with valid profile
        self.mock_client.get_company_profile.return_value = self.profile_1
        refresh_company(self.conn, "974760673", client=self.mock_client)

        # Second refresh fails due to timeout
        self.mock_client.get_company_profile.side_effect = RegistryClientError("Request timed out")
        result2 = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result2.status, "failed")
        self.assertIn("timed out", result2.error_message)

        # Original profile in DB must remain intact
        comp = get_company(self.conn, "974760673")
        self.assertEqual(comp["name"], "REGISTERENHETEN I BRØNNØYSUND")

    def test_conflicting_source_observation(self):
        """Test conflicting observation from a different source is flagged as 'conflict' in change report."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        refresh_company(self.conn, "974760673", client=self.mock_client, source_id="brreg_enhetsregisteret")

        # Conflicting profile from different source
        conflicting_raw = dict(self.sample_raw_1)
        conflicting_raw["antallAnsatte"] = 999
        conflicting_profile = NormalizedCompanyProfile.from_raw_dict(conflicting_raw)
        self.mock_client.get_company_profile.return_value = conflicting_profile

        result2 = refresh_company(
            self.conn, "974760673", client=self.mock_client, source_id="vendor_source", source_name="Third Party Vendor"
        )

        self.assertEqual(result2.status, "success")
        self.assertEqual(result2.summary_counts["conflict"], 1)

        conflict_item = [c for c in result2.changes if c.fact_key == "num_employees"][0]
        self.assertEqual(conflict_item.change_type, "conflict")
        self.assertEqual(conflict_item.previous_value, "487")
        self.assertEqual(conflict_item.new_value, "999")
        self.assertIn("Conflicting value '999'", conflict_item.explanation)

    def test_repeated_conflicting_refreshes_do_not_convert_to_updates(self):
        """Test that repeated refreshes from a non-primary conflicting source remain flagged as 'conflict' and do NOT convert to updates."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        refresh_company(self.conn, "974760673", client=self.mock_client, source_id="brreg_enhetsregisteret")

        # Conflicting profile from different source
        conflicting_raw = dict(self.sample_raw_1)
        conflicting_raw["antallAnsatte"] = 999
        conflicting_profile = NormalizedCompanyProfile.from_raw_dict(conflicting_raw)
        self.mock_client.get_company_profile.return_value = conflicting_profile

        # First conflicting refresh
        res2 = refresh_company(self.conn, "974760673", client=self.mock_client, source_id="vendor_source", source_name="Third Party Vendor")
        self.assertEqual(res2.summary_counts["conflict"], 1)

        # Second conflicting refresh from same non-primary source
        res3 = refresh_company(self.conn, "974760673", client=self.mock_client, source_id="vendor_source", source_name="Third Party Vendor")
        self.assertEqual(res3.summary_counts["conflict"], 1)
        self.assertEqual(res3.summary_counts["updated"], 0)

        # Fact value in DB MUST remain 487 (primary source value)
        facts = get_facts(self.conn, "974760673", active_only=True)
        emp_fact = [f for f in facts if f["fact_key"] == "num_employees"][0]
        self.assertEqual(emp_fact["fact_value"], "487")

    @patch("signal_post.refresh.save_company_profile")
    def test_db_commit_failure_returns_failed_result_and_rolls_back(self, mock_save):
        """Test that a database commit exception in save_company_profile returns status='failed' without reporting success."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        mock_save.side_effect = sqlite3.DatabaseError("Disk I/O error")

        result = refresh_company(self.conn, "974760673", client=self.mock_client)

        self.assertEqual(result.status, "failed")
        self.assertIn("Database transaction commit failed", result.error_message)

        # No company record should exist
        comp = get_company(self.conn, "974760673")
        self.assertIsNone(comp)

    def test_change_report_correctness_and_to_dict(self):
        """Test RefreshResult.to_dict() formatting for JSON output."""
        self.mock_client.get_company_profile.return_value = self.profile_1
        result = refresh_company(self.conn, "974760673", client=self.mock_client)

        d = result.to_dict()
        self.assertEqual(d["status"], "success")
        self.assertEqual(d["org_number"], "974760673")
        self.assertEqual(d["company_name"], "REGISTERENHETEN I BRØNNØYSUND")
        self.assertIsInstance(d["changes"], list)
        self.assertTrue(len(d["changes"]) > 0)


if __name__ == "__main__":
    unittest.main()
