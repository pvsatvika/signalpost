"""
Unit tests for Brønnøysund Corporate Group Structure (Konsernstruktur) Enrichment.
"""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.group_structure import (
    BrregGroupClient,
    GROUP_STATUS_BUDGET,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_MALFORMED,
    GROUP_STATUS_NO_GROUP,
    GROUP_STATUS_SUCCESS,
    GROUP_STRUCTURE_SOURCE_ID,
    extract_group_facts_and_evidence,
    save_group_facts_to_storage,
)
from signal_post.runner import CompanyRunner
from signal_post.storage import init_db


class TestGroupStructureClient(unittest.TestCase):
    def setUp(self):
        self.mock_session = MagicMock()
        self.client = BrregGroupClient(session=self.mock_session)

    def test_fetch_group_structure_success(self):
        sample_payload = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmenn aksjeselskap"},
            "children": [
                {
                    "organisasjonsnummer": "990888213",
                    "navn": "EQUINOR ENERGY AS",
                    "parentOrganisasjonsnummer": "923609016",
                    "parentNavn": "EQUINOR ASA",
                    "knytningsform": {"kode": "KDAT", "beskrivelse": "Konsern datter"},
                    "grunnlag": "100%",
                    "dato": "2025-11-12",
                    "nivaa": 1,
                    "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                    "children": [],
                }
            ],
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_payload
        self.mock_session.get.return_value = mock_resp

        res = self.client.fetch_group_structure("923609016")
        self.assertEqual(res.status, GROUP_STATUS_SUCCESS)
        self.assertEqual(res.http_status, 200)
        self.assertEqual(res.root_org_number, "923609016")
        self.assertEqual(res.root_name, "EQUINOR ASA")
        self.assertEqual(res.nodes_count, 2)
        self.assertTrue(res.is_complete_set)

    def test_fetch_group_structure_not_found_404(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        self.mock_session.get.return_value = mock_resp

        res = self.client.fetch_group_structure("999888777")
        self.assertEqual(res.status, GROUP_STATUS_NO_GROUP)
        self.assertEqual(res.http_status, 404)
        self.assertTrue(res.is_complete_set)

    def test_fetch_group_structure_http_500_failure(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        self.mock_session.get.return_value = mock_resp

        res = self.client.fetch_group_structure("923609016")
        self.assertEqual(res.status, GROUP_STATUS_FAILED)
        self.assertEqual(res.http_status, 500)
        self.assertFalse(res.is_complete_set)

    def test_fetch_group_structure_budget_exhausted(self):
        conn = sqlite3.connect(":memory:")
        budget_tracker = RequestBudgetTracker(conn, budget_limit=0)
        client = BrregGroupClient(session=self.mock_session, budget_tracker=budget_tracker)

        with self.assertRaises(RequestBudgetExceededError):
            client.fetch_group_structure("923609016")


class TestGroupFactExtraction(unittest.TestCase):
    def test_extract_multi_level_hierarchy(self):
        payload = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "children": [
                {
                    "organisasjonsnummer": "990888213",
                    "navn": "EQUINOR ENERGY AS",
                    "parentOrganisasjonsnummer": "923609016",
                    "parentNavn": "EQUINOR ASA",
                    "knytningsform": {"kode": "KDAT", "beskrivelse": "Konsern datter"},
                    "grunnlag": "100%",
                    "dato": "2025-11-12",
                    "nivaa": 1,
                    "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                    "children": [
                        {
                            "organisasjonsnummer": "980000111",
                            "navn": "SUB SUBSIDIARY AS",
                            "parentOrganisasjonsnummer": "990888213",
                            "parentNavn": "EQUINOR ENERGY AS",
                            "knytningsform": {"kode": "KDAT", "beskrivelse": "Konsern datter"},
                            "grunnlag": "Eier mer enn 50 %",
                            "dato": "2026-01-01",
                            "nivaa": 2,
                            "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
                            "children": [],
                        }
                    ],
                }
            ],
        }

        facts = extract_group_facts_and_evidence(
            payload, queried_org_number="990888213", source_url="https://test.url"
        )
        fact_keys = [k for k, _, _ in facts]
        self.assertIn("is_in_registered_group", fact_keys)
        self.assertIn("registered_group_root_org", fact_keys)
        self.assertIn("registered_group_node_count", fact_keys)

        rel1_key = "group_relation_923609016_990888213_KDAT"
        rel2_key = "group_relation_990888213_980000111_KDAT"
        self.assertIn(rel1_key, fact_keys)
        self.assertIn(rel2_key, fact_keys)

        # Verify exact JSON payload structure for relationship fact
        rel1_val_str = next(v for k, v, _ in facts if k == rel1_key)
        rel1_data = json.loads(rel1_val_str)
        self.assertEqual(rel1_data["parent_org"], "923609016")
        self.assertEqual(rel1_data["child_org"], "990888213")
        self.assertEqual(rel1_data["ownership_basis"], "100%")

    def test_cycle_protection(self):
        # Construct circular payload: node A -> child B -> child A
        node_a = {
            "organisasjonsnummer": "111111111",
            "navn": "CYCLE ROOT AS",
            "children": [],
        }
        node_b = {
            "organisasjonsnummer": "222222222",
            "navn": "CYCLE CHILD AS",
            "parentOrganisasjonsnummer": "111111111",
            "children": [node_a],  # Circular reference
        }
        node_a["children"] = [node_b]

        facts = extract_group_facts_and_evidence(
            node_a, queried_org_number="111111111", source_url="https://test.url"
        )
        # Verify execution completed without infinite recursion
        fact_keys = [k for k, _, _ in facts]
        self.assertIn("is_in_registered_group", fact_keys)


class TestGroupStorage(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_group_facts_and_removal(self):
        org = "923609016"
        sample_facts = [
            ("is_in_registered_group", "true", {"exact_source_url": "http://test", "retrieved_at": "2026-01-01T00:00:00Z"}),
            ("registered_group_root_org", "923609016", {"exact_source_url": "http://test", "retrieved_at": "2026-01-01T00:00:00Z"}),
            ("group_relation_923609016_990888213_KDAT", '{"basis": "100%"}', {"exact_source_url": "http://test", "retrieved_at": "2026-01-01T00:00:00Z"}),
        ]

        counts = save_group_facts_to_storage(self.conn, org, sample_facts)
        self.assertEqual(counts["created"], 3)

        # Check DB rows
        active_facts = self.conn.execute("SELECT fact_key FROM facts WHERE org_number=? AND is_active=1;", (org,)).fetchall()
        self.assertEqual(len(active_facts), 3)

        # Re-save empty set (e.g. company left group)
        counts2 = save_group_facts_to_storage(self.conn, org, [], complete_set=True)
        self.assertEqual(counts2["removed"], 3)

        active_after = self.conn.execute("SELECT fact_key FROM facts WHERE org_number=? AND is_active=1;", (org,)).fetchall()
        self.assertEqual(len(active_after), 0)


class TestRunnerGroupIntegration(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    @patch("signal_post.runner.refresh_company")
    @patch("signal_post.group_structure.BrregGroupClient.fetch_group_structure")
    def test_runner_process_company_with_group(self, mock_fetch_group, mock_refresh):
        mock_refresh_res = MagicMock()
        mock_refresh_res.status = "success"
        mock_refresh_res.company_name = "EQUINOR ASA"
        mock_refresh_res.changes = []
        mock_refresh.return_value = mock_refresh_res

        sample_payload = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "children": [
                {
                    "organisasjonsnummer": "990888213",
                    "navn": "EQUINOR ENERGY AS",
                    "parentOrganisasjonsnummer": "923609016",
                    "knytningsform": {"kode": "KDAT", "beskrivelse": "Konsern datter"},
                    "grunnlag": "100%",
                    "children": [],
                }
            ],
        }

        mock_grp_res = MagicMock()
        mock_grp_res.status = GROUP_STATUS_SUCCESS
        mock_grp_res.is_complete_set = True
        mock_grp_res.root_payload = sample_payload
        mock_grp_res.source_url = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/923609016"
        mock_grp_res.retrieved_at = "2026-01-01T00:00:00Z"
        mock_grp_res.error = None
        mock_fetch_group.return_value = mock_grp_res

        from signal_post.models import NormalizedCompanyProfile
        from signal_post.storage import save_company_profile

        save_company_profile(self.conn, NormalizedCompanyProfile(org_number="923609016", name="EQUINOR ASA"))

        runner = CompanyRunner(conn=self.conn)
        res = runner.process_company("923609016", include_group=True)

        self.assertEqual(res["source_status"]["group"], GROUP_STATUS_SUCCESS)
        fact_keys = [f["fact_key"] for f in res["facts"]]
        self.assertIn("is_in_registered_group", fact_keys)
        self.assertIn("group_relation_923609016_990888213_KDAT", fact_keys)


if __name__ == "__main__":
    unittest.main()
