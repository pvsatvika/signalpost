"""
Unit tests for official Brønnøysund Roles API integration, privacy redaction,
role fact normalization, evidence linkage, update/removal semantics, and runner metrics.
"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from signal_post.budget import RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.exceptions import RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.roles import (
    BrregRolesClient,
    ROLES_SOURCE_ID,
    ROLES_SOURCE_NAME,
    extract_normalized_roles,
    extract_role_facts_and_evidence,
    redact_person_privacy,
    save_role_facts_to_storage,
)
from signal_post.runner import CompanyRunner
from signal_post.storage import get_connection, get_facts, init_db, save_company_profile


class TestRolesIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_roles.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        # Pre-populate sample company profile
        self.profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
        })
        save_company_profile(self.conn, self.profile)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_privacy_redaction_fodselsdato(self):
        """Verify redact_person_privacy strips birth dates and national ID numbers from payloads."""
        raw_payload = {
            "rollegrupper": [
                {
                    "roller": [
                        {
                            "person": {
                                "navn": {"fornavn": "Anders", "etternavn": "Opedal"},
                                "fodselsdato": "1968-05-04",
                                "fnr": "04056812345",
                            }
                        }
                    ]
                }
            ]
        }
        cleaned = redact_person_privacy(raw_payload)
        person_cleaned = cleaned["rollegrupper"][0]["roller"][0]["person"]

        self.assertNotIn("fodselsdato", person_cleaned)
        self.assertNotIn("fnr", person_cleaned)
        self.assertEqual(person_cleaned["navn"]["fornavn"], "Anders")

    def test_person_and_org_role_normalization(self):
        """Verify person and organization role holders are parsed cleanly into normalized role items."""
        sample_roles_data = {
            "rollegrupper": [
                {
                    "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                    "sistEndret": "2020-11-02",
                    "roller": [
                        {
                            "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                            "avregistrert": False,
                            "rekkefolge": 0,
                            "person": {
                                "navn": {"fornavn": "Anders", "etternavn": "Opedal"},
                                "fodselsdato": "1968-05-04",
                            },
                        }
                    ],
                },
                {
                    "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                    "sistEndret": "2019-07-12",
                    "roller": [
                        {
                            "type": {"kode": "REVI", "beskrivelse": "Revisor"},
                            "avregistrert": False,
                            "rekkefolge": 0,
                            "enhet": {
                                "organisasjonsnummer": "976389387",
                                "navn": ["ERNST & YOUNG AS"],
                            },
                        }
                    ],
                },
            ]
        }

        normalized = extract_normalized_roles(sample_roles_data)
        self.assertEqual(len(normalized), 2)

        # First role item (DAGL)
        dagl = [r for r in normalized if r.role_code == "DAGL"][0]
        self.assertEqual(dagl.holder.holder_type, "person")
        self.assertEqual(dagl.holder.name, "Anders Opedal")
        self.assertIsNone(dagl.holder.org_number)

        # Second role item (REVI)
        revi = [r for r in normalized if r.role_code == "REVI"][0]
        self.assertEqual(revi.holder.holder_type, "organization")
        self.assertEqual(revi.holder.name, "ERNST & YOUNG AS")
        self.assertEqual(revi.holder.org_number, "976389387")

    def test_empty_and_malformed_roles_handling(self):
        """Verify empty role groups or malformed JSON payloads return empty list without crashing."""
        self.assertEqual(extract_normalized_roles({}), [])
        self.assertEqual(extract_normalized_roles({"rollegrupper": []}), [])
        self.assertEqual(extract_normalized_roles(None), [])

    def test_roles_client_404_and_timeout(self):
        """Verify BrregRolesClient handles 404 cleanly and returns failed status on network failure."""
        roles_client = BrregRolesClient()

        # Mock 404 response
        with patch.object(roles_client.client.session, "get") as mock_get:
            mock_get.return_value.status_code = 404
            res = roles_client.fetch_roles("974760673")
            self.assertEqual(res.status, "not_found")
            self.assertEqual(res.http_status, 404)
            self.assertIsNone(res.payload)

        # Mock network failure
        with patch.object(roles_client.client.session, "get") as mock_get:
            mock_get.side_effect = Exception("Mock timeout")
            res = roles_client.fetch_roles("974760673")
            self.assertEqual(res.status, "failed")
            self.assertIn("Mock timeout", res.error)

    def test_request_budget_exhaustion_in_roles_client(self):
        """Verify Roles API client respects request budget limits."""
        budget_tracker = RequestBudgetTracker(self.conn, budget_limit=1)
        budget_tracker.record_request("test", "http://test")

        roles_client = BrregRolesClient(budget_tracker=budget_tracker)
        with self.assertRaises(Exception) as ctx:
            roles_client.fetch_roles("923609016")
        self.assertIn("budget limit reached", str(ctx.exception).lower())

    def test_role_fact_storage_and_evidence_lineage(self):
        """Verify role facts are saved into SQLite with complete evidence lineage."""
        sample_payload = {
            "rollegrupper": [
                {
                    "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                    "sistEndret": "2020-11-02",
                    "roller": [
                        {
                            "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                            "avregistrert": False,
                            "rekkefolge": 0,
                            "person": {
                                "navn": {"fornavn": "Anders", "etternavn": "Opedal"},
                            },
                        }
                    ],
                }
            ]
        }

        extracted = extract_role_facts_and_evidence(sample_payload, "923609016")
        count = save_role_facts_to_storage(self.conn, "923609016", extracted)
        self.assertEqual(count["created"], 1)

        facts = get_facts(self.conn, "923609016", active_only=True)
        role_facts = [f for f in facts if f["fact_key"].startswith("role_")]
        self.assertEqual(len(role_facts), 1)

        f = role_facts[0]
        self.assertIn("role_dagl_dagl", f["fact_key"])
        self.assertIn("Anders Opedal", f["fact_value"])
        self.assertNotIn("fodselsdato", f["fact_value"])

    def test_role_removal_deactivates_fact(self):
        """Verify that when a role disappears from a subsequent complete roles response, old fact is deactivated."""
        initial_payload = {
            "rollegrupper": [
                {
                    "type": {"kode": "DAGL"},
                    "roller": [{"type": {"kode": "DAGL"}, "avregistrert": False, "person": {"navn": {"fornavn": "Old", "etternavn": "Leader"}}}]
                }
            ]
        }
        ex1 = extract_role_facts_and_evidence(initial_payload, "923609016")
        save_role_facts_to_storage(self.conn, "923609016", ex1)

        # Confirm 1 active role fact
        active1 = [f for f in get_facts(self.conn, "923609016", active_only=True) if f["fact_key"].startswith("role_")]
        self.assertEqual(len(active1), 1)

        # Empty roles payload (roles removed)
        empty_payload = {"rollegrupper": []}
        ex2 = extract_role_facts_and_evidence(empty_payload, "923609016")
        save_role_facts_to_storage(self.conn, "923609016", ex2)

        # Confirm 0 active role facts
        active2 = [f for f in get_facts(self.conn, "923609016", active_only=True) if f["fact_key"].startswith("role_")]
        self.assertEqual(len(active2), 0)

    def test_runner_base_company_succeeds_when_roles_fail(self):
        """Verify base company profile resolution succeeds even if Roles API call fails."""
        runner = CompanyRunner(self.conn)

        from signal_post.roles import RolesFetchResult
        with patch.object(BrregClient, "fetch_raw_company") as mock_base, \
             patch.object(BrregRolesClient, "fetch_roles") as mock_roles:

            mock_base.return_value = {
                "organisasjonsnummer": "923609016",
                "navn": "EQUINOR ASA",
                "organisasjonsform": {"kode": "ASA"},
            }
            mock_roles.return_value = RolesFetchResult(status="failed", error="Mock Roles API Timeout")

            res = runner.process_company("923609016", live_refresh=True, include_roles=True)

            self.assertEqual(res["status"], "live_refreshed")
            self.assertEqual(res["source_status"]["enhetsregisteret"], "success")
            self.assertEqual(res["source_status"]["roles"], "failed")
            self.assertGreater(len(res["warnings"]), 0)

    def test_batch_runner_coverage_metrics_reconciliation(self):
        """Verify run_metrics calculates role coverage statistics correctly."""
        runner = CompanyRunner(self.conn)

        from signal_post.roles import RolesFetchResult
        with patch.object(BrregClient, "fetch_raw_company") as mock_base, \
             patch.object(BrregRolesClient, "fetch_roles") as mock_roles:

            mock_base.return_value = {
                "organisasjonsnummer": "923609016",
                "navn": "EQUINOR ASA",
                "organisasjonsform": {"kode": "ASA"},
            }
            mock_roles.return_value = RolesFetchResult(
                status="success",
                http_status=200,
                payload={
                    "_links": {"self": {"href": "https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller"}},
                    "rollegrupper": [
                        {
                            "type": {"kode": "DAGL"},
                            "roller": [{"type": {"kode": "DAGL"}, "avregistrert": False, "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}}}]
                        }
                    ]
                }
            )

            report = runner.run_batch(["923609016"], live_refresh=True, include_roles=True)
            m = report["run_metrics"]

            self.assertEqual(m["success_count"], 1)
            self.assertEqual(m["companies_with_roles_count"], 1)
            self.assertEqual(m["companies_without_roles_count"], 0)
            self.assertEqual(m["total_role_facts"], 1)
            self.assertEqual(m["average_role_facts_per_company"], 1.0)
            self.assertEqual(m["estimated_external_api_cost"], "$0")
