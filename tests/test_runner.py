"""
Unit tests for evaluator-facing runner module, output serialization,
batch failure isolation, request-budget protection, and 100-company offline benchmark.
"""

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from signal_post.budget import RequestBudgetTracker
from signal_post.bulk import generate_bootstrap_manifest
from signal_post.client import BrregClient
from signal_post.exceptions import InvalidOrgNumberError, RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.runner import (
    CompanyRunner,
    compute_file_sha256,
    generate_benchmark_metadata,
    get_git_commit_hash,
    load_input_org_numbers,
    run_evaluation,
    validate_canonical_benchmark,
)
from signal_post.storage import get_connection, init_db, save_company_profile


class TestCompanyRunner(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_runner.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        # Pre-populate local DB with 2 sample company profiles
        self.profile1 = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
            "antallAnsatte": 21272,
            "registreringsdatoEnhetsregisteret": "1995-03-12",
        })
        save_company_profile(self.conn, self.profile1, exact_source_url="https://data.brreg.no/enhetsregisteret/api/enheter/923609016")

        self.profile2 = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "organisasjonsform": {"kode": "ORGL", "beskrivelse": "Organisasjonsledd"},
            "antallAnsatte": 487,
            "registreringsdatoEnhetsregisteret": "1995-08-09",
        })
        save_company_profile(self.conn, self.profile2, exact_source_url="https://data.brreg.no/enhetsregisteret/api/enheter/974760673")

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_single_company_runner_local_cache(self):
        """Test runner fetches stored profile from local SQLite database cleanly when live_refresh is False."""
        runner = CompanyRunner(self.conn)
        res = runner.process_company("923609016", live_refresh=False)

        self.assertEqual(res["org_number"], "923609016")
        self.assertEqual(res["name"], "EQUINOR ASA")
        self.assertEqual(res["status"], "served_from_local")
        self.assertGreater(len(res["facts"]), 0)

        # Verify fact evidence fields
        first_fact = res["facts"][0]
        self.assertIn("fact_key", first_fact)
        self.assertIn("value", first_fact)
        self.assertIn("source_name", first_fact)
        self.assertIn("source_url", first_fact)
        self.assertIn("retrieved_at", first_fact)
        self.assertIn("verification_status", first_fact)

    def test_single_company_invalid_org_number(self):
        """Test invalid organization number handling produces isolated failure without throwing exception."""
        runner = CompanyRunner(self.conn)
        res = runner.process_company("INVALID123", live_refresh=False)

        self.assertEqual(res["org_number"], "INVALID123")
        self.assertEqual(res["status"], "failed")
        self.assertEqual(len(res["facts"]), 0)
        self.assertGreater(len(res["errors"]), 0)
        self.assertIn("Invalid 9-digit", res["errors"][0])

    @patch.object(BrregClient, "fetch_raw_company")
    def test_single_company_live_refresh_success(self, mock_fetch):
        """Test live refresh succeeds, updates SQLite, and reports status='live_refreshed'."""
        mock_fetch.return_value = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA UPDATED",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
            "antallAnsatte": 22000,
        }

        runner = CompanyRunner(self.conn)
        res = runner.process_company("923609016", live_refresh=True)

        self.assertEqual(res["org_number"], "923609016")
        self.assertEqual(res["name"], "EQUINOR ASA UPDATED")
        self.assertEqual(res["status"], "live_refreshed")
        self.assertGreater(len(res["changes"]), 0)

    def test_mixed_success_failure_batch(self):
        """Test batch runner processes mixed list of valid and invalid orgs with 100% failure isolation."""
        runner = CompanyRunner(self.conn)
        report = runner.run_batch(["923609016", "INVALID999", "974760673"], live_refresh=False)

        metrics = report["run_metrics"]
        self.assertEqual(metrics["requested_count"], 3)
        self.assertEqual(metrics["processed_count"], 3)
        self.assertEqual(metrics["success_count"], 2)
        self.assertEqual(metrics["failed_count"], 1)
        self.assertEqual(metrics["estimated_external_api_cost"], "$0")

        # Verify ordering is preserved
        self.assertEqual(report["results"][0]["org_number"], "923609016")
        self.assertEqual(report["results"][1]["org_number"], "INVALID999")
        self.assertEqual(report["results"][2]["org_number"], "974760673")

    def test_request_budget_exhaustion_behavior(self):
        """Test batch halts new network requests when request budget is exhausted, falling back to cache or failure gracefully."""
        # Pre-exhaust request budget (limit: 1)
        budget_tracker = RequestBudgetTracker(self.conn, budget_limit=1)
        budget_tracker.record_request(request_type="company_fetch", target_url="https://data.brreg.no/test")
        self.assertEqual(budget_tracker.count_requests_today(), 1)
        self.assertFalse(budget_tracker.can_make_request())

        runner = CompanyRunner(self.conn, budget_tracker=budget_tracker)

        # Process company in local DB -> budget exhausted, served from local cache with warning
        res1 = runner.process_company("974760673", live_refresh=True)
        self.assertEqual(res1["status"], "local_fallback_after_refresh_failure")
        self.assertGreater(len(res1["warnings"]), 0)
        self.assertIn("Request budget exhausted", res1["warnings"][0])

        # Process company not in local DB -> fails gracefully with error message
        res2 = runner.process_company("999999999", live_refresh=True)
        self.assertEqual(res2["status"], "failed")
        self.assertGreater(len(res2["errors"]), 0)
        self.assertIn("budget exhausted", res2["errors"][0].lower())

    def test_load_input_org_numbers_formats(self):
        """Test loading organization numbers from JSON array file, dict JSON file, and line-delimited text."""
        # JSON Array
        p1 = Path(self.temp_dir.name) / "input1.json"
        p1.write_text('["923609016", "974760673"]', encoding="utf-8")
        self.assertEqual(load_input_org_numbers(str(p1)), ["923609016", "974760673"])

        # JSON Dict
        p2 = Path(self.temp_dir.name) / "input2.json"
        p2.write_text('{"org_numbers": ["810034882"]}', encoding="utf-8")
        self.assertEqual(load_input_org_numbers(str(p2)), ["810034882"])

        # Line delimited text
        p3 = Path(self.temp_dir.name) / "input3.txt"
        p3.write_text("923609016\n974760673\n", encoding="utf-8")
        self.assertEqual(load_input_org_numbers(str(p3)), ["923609016", "974760673"])

        # Empty file
        p4 = Path(self.temp_dir.name) / "empty.json"
        p4.write_text("", encoding="utf-8")
        self.assertEqual(load_input_org_numbers(str(p4)), [])

    def test_100_company_offline_benchmark(self):
        """
        100-Company Offline Benchmark Test:
        Simulates processing 100 organization numbers offline using pre-stored SQLite profiles.
        Verifies ordering, 100% success rate, metrics calculation, and schema serialization.
        """
        # Populate DB with 100 mock companies
        for i in range(100):
            org = f"9{i:08d}"
            prof = NormalizedCompanyProfile.from_raw_dict({
                "organisasjonsnummer": org,
                "navn": f"COMPANY {i} AS",
                "organisasjonsform": {"kode": "AS"},
                "antallAnsatte": i * 2,
            })
            save_company_profile(self.conn, prof)

        target_orgs = [f"9{i:08d}" for i in range(100)]

        runner = CompanyRunner(self.conn)
        report = runner.run_batch(target_orgs, live_refresh=False)

        metrics = report["run_metrics"]
        self.assertEqual(metrics["requested_count"], 100)
        self.assertEqual(metrics["processed_count"], 100)
        self.assertEqual(metrics["success_count"], 100)
        self.assertEqual(metrics["failed_count"], 0)
        self.assertEqual(metrics["served_from_local_count"], 100)
        self.assertEqual(metrics["total_outbound_requests"], 0)
        self.assertEqual(metrics["estimated_external_api_cost"], "$0")

        # Verify JSON serialization works deterministically
        serialized = json.dumps(report, indent=2, ensure_ascii=False)
        deserialized = json.loads(serialized)
        self.assertEqual(len(deserialized["results"]), 100)

        # Verify exact ordering preserved
        for i in range(100):
            self.assertEqual(deserialized["results"][i]["org_number"], target_orgs[i])

    def test_manifest_metadata_completeness(self):
        """Verify bootstrap manifest generation populated all required audit metadata fields."""
        dummy_file = str(Path(self.temp_dir.name) / "dummy.json.gz")
        Path(dummy_file).write_bytes(b"")

        # Mock select_bootstrap_candidates
        with patch("signal_post.bulk.select_bootstrap_candidates") as mock_select:
            mock_select.return_value = (["923609016"], {
                "schema_version": "1.0.0",
                "official_bulk_source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/lastned",
                "source_id": "brreg_bulk_enhetsregisteret",
                "local_source_file": "dummy.json.gz",
                "bulk_snapshot_last_modified": "Mon Oct 05 04:27:34 CEST 2026",
                "generated_at": "2026-10-05T14:00:00Z",
                "selection_algorithm_name": "deterministic_proportional_org_form_allocation",
                "selection_algorithm_version": "1.0.0",
                "deterministic_seed": None,
                "random_seed_required": False,
                "requested_count": 1,
                "actual_count": 1,
                "org_form_distribution": {"AS": 1},
                "selected_org_numbers": ["923609016"],
            })

            out_manifest = str(Path(self.temp_dir.name) / "manifest.json")
            manifest = generate_bootstrap_manifest(dummy_file, out_manifest, target_count=1)

            self.assertEqual(manifest["schema_version"], "1.0.0")
            self.assertEqual(manifest["selection_algorithm_name"], "deterministic_proportional_org_form_allocation")
            self.assertFalse(manifest["random_seed_required"])
            self.assertIn("selected_org_numbers", manifest)

    def test_summary_wording_terminology(self):
        """Verify summary uses non-misleading 'source-supported' terminology instead of 'verified'."""
        runner = CompanyRunner(self.conn)
        res = runner.process_company("923609016", live_refresh=False)
        self.assertIn("active source-supported facts", res["summary"])
        self.assertNotIn("verified active facts", res["summary"])

    def test_source_validity_date_separation(self):
        """Verify source_validity_date is None for REST API facts when omitted, and retrieved_at is populated."""
        runner = CompanyRunner(self.conn)
        res = runner.process_company("923609016", live_refresh=False)
        facts = res["facts"]
        self.assertGreater(len(facts), 0)
        for f in facts:
            self.assertIsNotNone(f["retrieved_at"])
            # For REST API company profile where no explicit dataset date header was supplied:
            self.assertIsNone(f["source_validity_date"])

    def test_metric_reconciliation(self):
        """Verify run metrics mathematically reconcile across requested, processed, success, and failure counts."""
        runner = CompanyRunner(self.conn)
        report = runner.run_batch(["923609016", "INVALID123"], live_refresh=False)
        m = report["run_metrics"]

        self.assertEqual(m["requested_count"], 2)
        self.assertEqual(m["processed_count"], 2)
        self.assertEqual(m["processed_count"], m["success_count"] + m["failed_count"])
        self.assertEqual(m["success_count"], m["served_from_local_count"] + m["live_refreshed_count"] + m["local_fallback_count"])

    def test_local_fallback_status(self):
        """Verify status='local_fallback_after_refresh_failure' is set when live refresh fails but local DB profile exists."""
        runner = CompanyRunner(self.conn)
        with patch.object(BrregClient, "fetch_raw_company") as mock_fetch:
            mock_fetch.side_effect = RegistryClientError("Mock HTTP timeout")
            res = runner.process_company("923609016", live_refresh=True)

            self.assertEqual(res["status"], "local_fallback_after_refresh_failure")
            self.assertGreater(len(res["warnings"]), 0)
            self.assertIn("Live refresh failed", res["warnings"][0])

    def test_offline_live_run_mode_correctness(self):
        """Verify offline mode sets run_mode='offline', served_from_local status, and zero request count."""
        runner = CompanyRunner(self.conn)
        report = runner.run_batch(["923609016"], live_refresh=False)
        m = report["run_metrics"]

        self.assertEqual(m["run_mode"], "offline")
        self.assertEqual(m["total_outbound_requests"], 0)
        self.assertEqual(m["live_refreshed_count"], 0)
        self.assertEqual(m["served_from_local_count"], 1)
        self.assertEqual(report["results"][0]["status"], "served_from_local")

    def test_run_level_request_delta_with_preexisting_rows(self):
        """Verify per-run request count measures delta correctly even when pre-existing request_log rows exist in database."""
        budget_tracker = RequestBudgetTracker(self.conn, budget_limit=100)

        # Insert 10 pre-existing dummy request rows into request_log
        for i in range(10):
            budget_tracker.record_request(request_type="preexisting_test", target_url=f"https://data.brreg.no/old_{i}")

        self.assertEqual(budget_tracker.get_today_request_count(), 10)

        # Run batch with mock live refresh making 1 request
        runner = CompanyRunner(self.conn, budget_tracker=budget_tracker)
        def mock_fetch_impl(url_or_org):
            budget_tracker.record_request("company_fetch", "https://data.brreg.no/test")
            return {
                "organisasjonsnummer": "923609016",
                "navn": "EQUINOR ASA REFRESHED",
                "organisasjonsform": {"kode": "ASA"},
            }

        with patch.object(BrregClient, "fetch_raw_company", side_effect=mock_fetch_impl):
            report = runner.run_batch(["923609016"], live_refresh=True)

        m = report["run_metrics"]
        self.assertEqual(m["total_outbound_requests"], 1)
        self.assertEqual(budget_tracker.get_today_request_count(), 11)

    def test_benchmark_metadata_generation_and_sha256(self):
        """Verify SHA-256 calculation and immutable benchmark metadata generation."""
        input_file = Path(self.temp_dir.name) / "input.json"
        input_file.write_text('["923609016"]', encoding="utf-8")

        output_file = Path(self.temp_dir.name) / "output.json"
        dummy_report = {
            "run_metrics": {
                "requested_count": 1,
                "processed_count": 1,
                "success_count": 1,
                "failed_count": 0,
                "served_from_local_count": 1,
                "local_fallback_count": 0,
                "live_refreshed_count": 0,
                "total_outbound_requests": 0,
                "elapsed_seconds": 0.12,
                "estimated_external_api_cost": "$0",
            },
            "results": [
                {
                    "org_number": "923609016",
                    "name": "EQUINOR ASA",
                    "status": "served_from_local",
                    "facts": [
                        {
                            "fact_key": "name",
                            "value": "EQUINOR ASA",
                            "source_name": "Brønnøysund Register Centre - Enhetsregisteret",
                            "source_url": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
                            "retrieved_at": "2026-10-05T15:00:00Z",
                            "verification_status": "source_asserted",
                        }
                    ],
                    "summary": "Company 'EQUINOR ASA' (923609016): 1 active source-supported facts. Loaded from local SQLite cache.",
                }
            ],
        }
        output_file.write_text(json.dumps(dummy_report, indent=2), encoding="utf-8")

        meta_file = Path(self.temp_dir.name) / "metadata.json"
        cmd = "python -m signal_post --run input.json --output output.json --db test.db"

        meta = generate_benchmark_metadata(
            input_file=str(input_file),
            output_file=str(output_file),
            db_file=self.db_path,
            command_executed=cmd,
            output_metadata_file=str(meta_file),
        )

        self.assertTrue(meta_file.exists())
        self.assertEqual(meta["input_sha256"], compute_file_sha256(input_file))
        self.assertEqual(meta["output_sha256"], compute_file_sha256(output_file))
        self.assertEqual(meta["db_integrity_status"], "ok")
        self.assertEqual(meta["foreign_key_violations"], 0)
        self.assertEqual(meta["command_executed"], cmd)
