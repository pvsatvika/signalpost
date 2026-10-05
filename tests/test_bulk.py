"""
Unit tests for bulk bootstrap importer, dataset streaming, memory-efficient JSON parsing,
request budget enforcement, and conservative live profile protection.
"""

import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError
from signal_post.bulk import (
    BULK_SOURCE_ID,
    OFFICIAL_BULK_DOWNLOAD_URL,
    analyze_bulk_org_forms,
    download_bulk_dataset,
    generate_bootstrap_manifest,
    import_bulk_from_manifest,
    import_bulk_profiles,
    select_bootstrap_candidates,
    stream_bulk_file,
    stream_json_objects,
    validate_database_integrity,
)
from signal_post.client import BrregClient
from signal_post.exceptions import RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.refresh import refresh_company
from signal_post.storage import (
    DEFAULT_SOURCE_ID,
    get_company,
    get_connection,
    get_evidence_for_fact,
    get_facts,
    init_db,
    save_company_profile,
)


class TestBulkImporter(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_bulk.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.sample_records = [
            {
                "organisasjonsnummer": "974760673",
                "navn": "REGISTERENHETEN I BRØNNØYSUND",
                "organisasjonsform": {"kode": "ORGL", "beskrivelse": "Organisasjonsledd"},
                "antallAnsatte": 487,
                "registreringsdatoEnhetsregisteret": "1995-08-09",
            },
            {
                "organisasjonsnummer": "923609016",
                "navn": "EQUINOR ASA",
                "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
                "antallAnsatte": 21272,
                "registreringsdatoEnhetsregisteret": "1995-03-12",
            },
            {
                "organisasjonsnummer": "989061593",
                "navn": "-G- GROUP AS",
                "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                "registreringsdatoEnhetsregisteret": "2006-01-23",
            },
            {
                "organisasjonsnummer": "INVALID123",  # Invalid org number
                "navn": "BAD ORG",
            },
            {
                "organisasjonsnummer": "974760673",  # Duplicate org number
                "navn": "REGISTERENHETEN I BRØNNØYSUND",
            },
            {
                "organisasjonsnummer": "995849364",
                "navn": ":-) INVEST AS",
                "organisasjonsform": {"kode": "AS"},
            },
            {
                "organisasjonsnummer": "929943945",
                "navn": "!FALSE INVESTMENTS AS",
                "organisasjonsform": {"kode": "AS"},
            },
        ]

        # Write sample records to plain JSON file
        self.json_file_path = str(Path(self.temp_dir.name) / "sample_bulk.json")
        with open(self.json_file_path, "w", encoding="utf-8") as f:
            json.dump(self.sample_records, f, ensure_ascii=False)

        # Write sample records to gzipped JSON file
        self.gz_file_path = str(Path(self.temp_dir.name) / "sample_bulk.json.gz")
        with gzip.open(self.gz_file_path, "wt", encoding="utf-8") as f:
            json.dump(self.sample_records, f, ensure_ascii=False)

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_streaming_json_parser(self):
        """Test stream_bulk_file parses JSON objects without loading whole file into RAM."""
        items = list(stream_bulk_file(self.json_file_path))
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]["organisasjonsnummer"], "974760673")
        self.assertEqual(items[1]["organisasjonsnummer"], "923609016")

    def test_gzipped_streaming_json_parser(self):
        """Test streaming from gzipped .json.gz file."""
        items = list(stream_bulk_file(self.gz_file_path))
        self.assertEqual(len(items), 7)
        self.assertEqual(items[0]["organisasjonsnummer"], "974760673")

    def test_bounded_bulk_import_and_deduplication(self):
        """Test importing bounded profile count with org validation and deduplication."""
        summary = import_bulk_profiles(self.conn, self.json_file_path, limit=5)

        self.assertEqual(summary["imported_count"], 5)
        self.assertEqual(summary["invalid_skipped"], 1)
        self.assertEqual(summary["stopped_reason"], "limit_reached")

        comp1 = get_company(self.conn, "974760673")
        comp2 = get_company(self.conn, "923609016")
        comp3 = get_company(self.conn, "989061593")
        self.assertIsNotNone(comp1)
        self.assertIsNotNone(comp2)
        self.assertIsNotNone(comp3)

        # Check stored facts evidence source
        facts1 = get_facts(self.conn, "974760673", active_only=True)
        self.assertGreater(len(facts1), 0)
        evidence1 = get_evidence_for_fact(self.conn, facts1[0]["fact_id"])
        self.assertEqual(evidence1[0]["source_id"], BULK_SOURCE_ID)
        self.assertIn("lastned", evidence1[0]["exact_source_url"])

    def test_bulk_import_filter_org_form(self):
        """Test bulk import with organization form filtering (e.g. 'AS')."""
        summary = import_bulk_profiles(self.conn, self.json_file_path, limit=10, filter_org_form="AS")

        # 3 AS companies in sample (989061593, 995849364, 929943945)
        self.assertEqual(summary["imported_count"], 3)

        comp = get_company(self.conn, "989061593")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["organization_form_code"], "AS")

    def test_conservative_live_profile_protection(self):
        """Test that importing older bulk data does NOT overwrite newer live profile facts."""
        # 1. Pre-seed profile 974760673 from live API (employees=487)
        raw_live = {
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 487,
        }
        live_profile = NormalizedCompanyProfile.from_raw_dict(raw_live)
        save_company_profile(
            self.conn,
            profile=live_profile,
            source_id=DEFAULT_SOURCE_ID,
            exact_source_url="https://data.brreg.no/enhetsregisteret/api/enheter/974760673",
            retrieved_at="2026-10-05T12:00:00Z",
        )

        # 2. Import bulk dataset where 974760673 has employees=450 (older)
        bulk_record = [{
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 450,
        }]
        bulk_file = str(Path(self.temp_dir.name) / "bulk_old.json")
        with open(bulk_file, "w", encoding="utf-8") as f:
            json.dump(bulk_record, f)

        summary = import_bulk_profiles(self.conn, bulk_file, limit=1)

        # 3. Verify live fact (employees=487) is preserved as active
        facts = get_facts(self.conn, "974760673", active_only=True)
        emp_fact = next((f for f in facts if f["fact_key"] == "num_employees"), None)
        self.assertIsNotNone(emp_fact)
        self.assertEqual(emp_fact["fact_value"], "487")

    @patch("signal_post.bulk.requests.get")
    def test_download_bulk_dataset_budget_enforcement(self, mock_get):
        """Test downloading bulk dataset enforces outbound HTTP request budget."""
        tracker = RequestBudgetTracker(self.conn, budget_limit=1)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"Last-Modified": "Mon, 05 Oct 2026 04:27:34 GMT"}
        mock_resp.iter_content.return_value = [b"chunk1", b"chunk2"]
        mock_get.return_value = mock_resp

        out_file = str(Path(self.temp_dir.name) / "downloaded.json.gz")
        summary = download_bulk_dataset(out_file, budget_tracker=tracker)

        self.assertEqual(summary["requests_made"], 1)
        self.assertEqual(tracker.count_requests_today(), 1)
        self.assertTrue(Path(out_file).exists())

        # Second download attempt with exhausted budget raises RequestBudgetExceededError
        with self.assertRaises(RequestBudgetExceededError):
            download_bulk_dataset(out_file, budget_tracker=tracker)

    def test_local_file_import_zero_network_requests(self):
        """Test importing from local bulk file uses 0 HTTP requests and consumes no budget."""
        tracker = RequestBudgetTracker(self.conn, budget_limit=10)
        initial_requests = tracker.count_requests_today()

        summary = import_bulk_profiles(self.conn, self.json_file_path, limit=2)
        self.assertEqual(summary["imported_count"], 2)

        # Budget count must remain unchanged
        self.assertEqual(tracker.count_requests_today(), initial_requests)

    def test_compatibility_with_refresh_workflow(self):
        """Test company imported via bulk can be seamlessly refreshed via live refresh workflow."""
        import_bulk_profiles(self.conn, self.json_file_path, limit=1)

        comp = get_company(self.conn, "974760673")
        self.assertIsNotNone(comp)

        mock_client = MagicMock()
        mock_profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 500,
        })
        mock_client.get_company_profile.return_value = mock_profile

        res = refresh_company(self.conn, "974760673", client=mock_client)
        self.assertEqual(res.status, "success")

        updated_comp = get_company(self.conn, "974760673")
        self.assertEqual(updated_comp["num_employees"], 500)

    def test_deterministic_selection_and_manifest_reproducibility(self):
        """Test selection algorithm produces identical reproducible order and manifest."""
        manifest_path = str(Path(self.temp_dir.name) / "test_manifest.json")
        manifest1 = generate_bootstrap_manifest(self.json_file_path, manifest_path, target_count=4)

        self.assertEqual(manifest1["requested_count"], 4)
        self.assertEqual(manifest1["actual_count"], 4)
        self.assertIn("AS", manifest1["org_form_distribution"])
        self.assertIn("ORGL", manifest1["org_form_distribution"])

        # Repeat selection algorithm
        manifest_path2 = str(Path(self.temp_dir.name) / "test_manifest2.json")
        manifest2 = generate_bootstrap_manifest(self.json_file_path, manifest_path2, target_count=4)

        # Exact matching organization number order
        self.assertEqual(manifest1["selected_org_numbers"], manifest2["selected_org_numbers"])
        self.assertEqual(manifest1["org_form_distribution"], manifest2["org_form_distribution"])

    def test_import_from_manifest_zero_network(self):
        """Test import_bulk_from_manifest imports target orgs with zero network calls."""
        manifest_path = str(Path(self.temp_dir.name) / "test_manifest.json")
        generate_bootstrap_manifest(self.json_file_path, manifest_path, target_count=3)

        tracker = RequestBudgetTracker(self.conn, budget_limit=10)
        initial_requests = tracker.count_requests_today()

        summary = import_bulk_from_manifest(self.conn, manifest_path, self.json_file_path)

        self.assertEqual(summary["imported_count"], 3)
        self.assertEqual(summary["unmatched_manifest_orgs"], 0)
        self.assertEqual(tracker.count_requests_today(), initial_requests)

    def test_database_integrity_validation(self):
        """Test validate_database_integrity reports integrity check, fact linkage, and stats."""
        import_bulk_profiles(self.conn, self.json_file_path, limit=3)

        report = validate_database_integrity(self.conn, expected_company_count=3)

        self.assertTrue(report["integrity_check_passed"])
        self.assertEqual(report["foreign_key_violations"], 0)
        self.assertEqual(report["total_companies"], 3)
        self.assertTrue(report["company_count_matched"])
        self.assertEqual(report["invalid_org_numbers_found"], 0)
        self.assertEqual(report["unlinked_active_facts"], 0)
        self.assertGreater(report["avg_facts_per_company"], 0)


if __name__ == "__main__":
    unittest.main()
