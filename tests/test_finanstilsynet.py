"""
Unit tests for Finanstilsynet Virksomhetsregisteret API v2 integration, privacy sanitization,
regulatory fact extraction, evidence linkage, no-match semantics, and runner metrics.
"""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from signal_post.budget import RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.finanstilsynet import (
    FinanstilsynetClient,
    FINANSTILSYNET_SOURCE_ID,
    FINANSTILSYNET_SOURCE_NAME,
    extract_finanstilsynet_facts_and_evidence,
    sanitize_finanstilsynet_privacy,
    save_finanstilsynet_facts_to_storage,
)
from signal_post.models import NormalizedCompanyProfile
from signal_post.roles import BrregRolesClient
from signal_post.runner import CompanyRunner
from signal_post.storage import get_connection, get_facts, init_db, save_company_profile


class TestFinanstilsynetIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.temp_dir.name) / "test_ft.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        # Pre-populate sample company profile
        self.profile = NormalizedCompanyProfile.from_raw_dict({
            "organisasjonsnummer": "984851006",
            "navn": "DNB BANK ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Aksjeselskap"},
        })
        save_company_profile(self.conn, self.profile)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_privacy_sanitization(self):
        """Verify sanitize_finanstilsynet_privacy removes sensitive personal/residential fields."""
        raw_payload = {
            "legalEntityId": 1234,
            "organisationNumber": "984851006",
            "name": "DNB BANK ASA",
            "fodselsdato": "1980-01-01",
            "fnr": "01018012345",
            "residentialAddress": "Secret Street 1",
            "licences": [
                {
                    "licenceType": {"code": "BANK", "name": {"norwegian": "Bank"}},
                    "birthDate": "1990-05-05",
                }
            ]
        }
        cleaned = sanitize_finanstilsynet_privacy(raw_payload)

        self.assertNotIn("fodselsdato", cleaned)
        self.assertNotIn("fnr", cleaned)
        self.assertNotIn("residentialAddress", cleaned)
        self.assertEqual(cleaned["organisationNumber"], "984851006")
        self.assertNotIn("birthDate", cleaned["licences"][0])

    def test_exact_company_match_and_fact_extraction(self):
        """Verify valid company query returns success and extracts Finanstilsynet ID, LEI code, and licences."""
        sample_entity = {
            "legalEntityId": 98973,
            "legalEntityType": "Company",
            "finanstilsynetId": "FT00068800",
            "organisationNumber": "984851006",
            "leiCode": "549300GKFG0RYRRQ1414",
            "name": "DNB BANK ASA",
            "licences": [
                {
                    "licensedEntity": {"legalEntityId": 98973, "legalEntityName": "DNB BANK ASA"},
                    "serviceProviderId": 98973,
                    "licenceType": {
                        "code": "BANK",
                        "name": {"norwegian": "Bank", "english": "Bank"},
                        "description": {"norwegian": "Lov om finansforetak"},
                    },
                    "registeredDate": "2000-01-01T00:00:00",
                    "serviceProviderType": "Licensed",
                    "supervisoryAuthority": {"legalEntityName": "FINANSTILSYNET"},
                }
            ]
        }

        facts = extract_finanstilsynet_facts_and_evidence(sample_entity, "984851006")
        self.assertEqual(len(facts), 3)

        keys = [f[0] for f in facts]
        self.assertIn("finanstilsynet_id", keys)
        self.assertIn("lei_code", keys)

        lic_fact = [f for f in facts if f[0].startswith("licence_")][0]
        self.assertEqual(lic_fact[0], "licence_bank_98973")
        self.assertIn("BANK", lic_fact[1])
        self.assertIn("Bank", lic_fact[1])
        self.assertEqual(lic_fact[2]["source_id"], FINANSTILSYNET_SOURCE_ID)

    def test_no_match_semantics(self):
        """Verify no-hit query returns status='not_registered' without throwing error."""
        ft_client = FinanstilsynetClient()

        with patch.object(ft_client.session, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "page": 1, "total": 0, "hitsReturned": 0, "legalEntities": []
            }
            res = ft_client.fetch_registry("923609016")
            self.assertEqual(res.status, "not_registered")
            self.assertIsNone(res.entity_data)

    def test_person_record_rejection(self):
        """Verify person records with legalEntityType='Person' are rejected."""
        ft_client = FinanstilsynetClient()

        with patch.object(ft_client.session, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "page": 1, "total": 1, "hitsReturned": 1,
                "legalEntities": [
                    {
                        "legalEntityId": 111,
                        "legalEntityType": "Person",
                        "organisationNumber": "984851006",
                        "name": "Person Name",
                    }
                ]
            }
            res = ft_client.fetch_registry("984851006")
            self.assertEqual(res.status, "not_registered")

    def test_org_number_mismatch_rejection(self):
        """Verify entities with mismatching organisationNumber are rejected."""
        ft_client = FinanstilsynetClient()

        with patch.object(ft_client.session, "get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = {
                "page": 1, "total": 1, "hitsReturned": 1,
                "legalEntities": [
                    {
                        "legalEntityId": 222,
                        "legalEntityType": "Company",
                        "organisationNumber": "999999999",
                        "name": "Other Corp",
                    }
                ]
            }
            res = ft_client.fetch_registry("984851006")
            self.assertEqual(res.status, "not_registered")

    def test_404_and_timeout(self):
        """Verify 404 returns 'not_found' and timeout returns 'failed'."""
        ft_client = FinanstilsynetClient()

        # 404
        with patch.object(ft_client.session, "get") as mock_get:
            mock_get.return_value.status_code = 404
            res = ft_client.fetch_registry("984851006")
            self.assertEqual(res.status, "not_found")

        # Timeout
        with patch.object(ft_client.session, "get") as mock_get:
            mock_get.side_effect = Exception("Mock Timeout")
            res = ft_client.fetch_registry("984851006")
            self.assertEqual(res.status, "failed")
            self.assertIn("Mock Timeout", res.error)

    def test_request_budget_exhaustion(self):
        """Verify request budget tracker raises RequestBudgetExceededError when limit reached."""
        tracker = RequestBudgetTracker(self.conn, budget_limit=1)
        tracker.record_request("test", "http://test")

        ft_client = FinanstilsynetClient(budget_tracker=tracker)
        with self.assertRaises(Exception) as ctx:
            ft_client.fetch_registry("984851006")
        self.assertIn("budget limit reached", str(ctx.exception).lower())

    def test_fact_storage_and_evidence_lineage(self):
        """Verify regulatory facts are saved into SQLite with complete evidence lineage."""
        sample_entity = {
            "legalEntityId": 98973,
            "legalEntityType": "Company",
            "finanstilsynetId": "FT00068800",
            "organisationNumber": "984851006",
            "name": "DNB BANK ASA",
            "licences": [
                {
                    "licensedEntity": {"legalEntityId": 98973},
                    "licenceType": {"code": "BANK", "name": {"norwegian": "Bank"}},
                }
            ]
        }

        extracted = extract_finanstilsynet_facts_and_evidence(sample_entity, "984851006")
        counts = save_finanstilsynet_facts_to_storage(self.conn, "984851006", extracted)
        self.assertEqual(counts["created"], 2)

        facts = get_facts(self.conn, "984851006", active_only=True)
        ft_facts = [f for f in facts if f["fact_key"].startswith("licence_") or f["fact_key"] == "finanstilsynet_id"]
        self.assertEqual(len(ft_facts), 2)

    def test_licence_disappearance_deactivates_fact(self):
        """Verify when a licence is missing on subsequent complete fetch, it is marked no_longer_present_in_current_registry."""
        sample_entity = {
            "legalEntityId": 98973,
            "legalEntityType": "Company",
            "finanstilsynetId": "FT00068800",
            "organisationNumber": "984851006",
            "name": "DNB BANK ASA",
            "licences": [
                {
                    "licensedEntity": {"legalEntityId": 98973},
                    "licenceType": {"code": "BANK", "name": {"norwegian": "Bank"}},
                }
            ]
        }

        ex1 = extract_finanstilsynet_facts_and_evidence(sample_entity, "984851006")
        save_finanstilsynet_facts_to_storage(self.conn, "984851006", ex1)

        # Confirm active fact
        active1 = [f for f in get_facts(self.conn, "984851006", active_only=True) if f["fact_key"].startswith("licence_")]
        self.assertEqual(len(active1), 1)

        # Later fetch returns entity with 0 licences
        save_finanstilsynet_facts_to_storage(self.conn, "984851006", [], complete_set=True)

        active2 = [f for f in get_facts(self.conn, "984851006", active_only=True) if f["fact_key"].startswith("licence_")]
        self.assertEqual(len(active2), 0)

    def test_runner_fault_isolation(self):
        """Verify Finanstilsynet API failure does not invalidate base registry or roles results."""
        runner = CompanyRunner(self.conn)

        from signal_post.finanstilsynet import FinanstilsynetFetchResult
        from signal_post.roles import RolesFetchResult

        with patch.object(BrregClient, "fetch_raw_company") as mock_base, \
             patch.object(BrregRolesClient, "fetch_roles") as mock_roles, \
             patch.object(runner.ft_client, "fetch_registry") as mock_ft:

            mock_base.return_value = {
                "organisasjonsnummer": "984851006",
                "navn": "DNB BANK ASA",
                "organisasjonsform": {"kode": "ASA"},
            }
            mock_roles.return_value = RolesFetchResult(status="success", payload={"rollegrupper": []})
            mock_ft.return_value = FinanstilsynetFetchResult(status="failed", error="Mock FT Error")

            res = runner.process_company("984851006", live_refresh=True, include_roles=True, include_finanstilsynet=True)

            self.assertEqual(res["status"], "live_refreshed")
            self.assertEqual(res["source_status"]["enhetsregisteret"], "success")
            self.assertEqual(res["source_status"]["roles"], "success")
            self.assertEqual(res["source_status"]["finanstilsynet"], "failed")

    def test_batch_runner_metrics(self):
        """Verify run_metrics calculates Finanstilsynet coverage statistics accurately."""
        runner = CompanyRunner(self.conn)

        from signal_post.finanstilsynet import FinanstilsynetFetchResult
        from signal_post.roles import RolesFetchResult

        with patch.object(BrregClient, "fetch_raw_company") as mock_base, \
             patch.object(BrregRolesClient, "fetch_roles") as mock_roles, \
             patch.object(runner.ft_client, "fetch_registry") as mock_ft:

            mock_base.return_value = {
                "organisasjonsnummer": "984851006",
                "navn": "DNB BANK ASA",
                "organisasjonsform": {"kode": "ASA"},
            }
            mock_roles.return_value = RolesFetchResult(status="success", payload={"rollegrupper": []})
            mock_ft.return_value = FinanstilsynetFetchResult(
                status="success",
                entity_data={
                    "legalEntityId": 98973,
                    "legalEntityType": "Company",
                    "finanstilsynetId": "FT00068800",
                    "organisationNumber": "984851006",
                    "name": "DNB BANK ASA",
                    "licences": [
                        {
                            "licensedEntity": {"legalEntityId": 98973},
                            "licenceType": {"code": "BANK", "name": {"norwegian": "Bank"}},
                        }
                    ]
                }
            )

            report = runner.run_batch(["984851006"], live_refresh=True, include_roles=True, include_finanstilsynet=True)
            m = report["run_metrics"]

            self.assertEqual(m["success_count"], 1)
            self.assertEqual(m["companies_with_regulatory_facts_count"], 1)
            self.assertEqual(m["total_regulatory_facts"], 2)  # ft_id + licence_bank_98973
            self.assertEqual(m["average_regulatory_facts_per_company"], 2.0)
            self.assertEqual(m["estimated_external_api_cost"], "$0")
