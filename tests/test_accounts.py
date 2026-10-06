"""
Unit tests for Regnskapsregisteret financial key figures integration.
"""

import json
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from signal_post.accounts import (
    ACCOUNTS_SOURCE_ID,
    ACCOUNTS_STATUS_EMPTY,
    ACCOUNTS_STATUS_FAILED,
    ACCOUNTS_STATUS_MALFORMED,
    ACCOUNTS_STATUS_NOT_FOUND,
    ACCOUNTS_STATUS_SUCCESS,
    AccountsFetchResult,
    RegnskapsClient,
    extract_account_facts_and_evidence,
    save_account_facts_to_storage,
)
from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.runner import CompanyRunner
from signal_post.storage import get_facts, init_db, save_company_profile
from signal_post.models import NormalizedCompanyProfile, Address


SAMPLE_ACCOUNTS_PAYLOAD = [
    {
        "id": 1,
        "journalnr": "2024100200",
        "regnskapstype": "SELSKAP",
        "virksomhet": {"organisasjonsnummer": "923609016"},
        "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
        "valuta": "NOK",
        "resultatregnskapResultat": {
            "driftsresultat": {
                "driftsinntekter": {
                    "salgsinntekter": 350000000000,
                    "sumDriftsinntekter": 360000000000,
                },
                "driftskostnad": {
                    "sumDriftskostnad": 280000000000,
                },
                "driftsresultat": 80000000000,
            },
            "ordinaertResultatFoerSkattekostnad": 75000000000,
            "aarsresultat": 50000000000,
        },
        "eiendeler": {
            "sumEiendeler": 500000000000,
            "omloepsmidler": {"sumOmloepsmidler": 150000000000},
            "anleggsmidler": {"sumAnleggsmidler": 350000000000},
        },
        "egenkapitalGjeld": {
            "egenkapital": {"sumEgenkapital": 200000000000},
            "gjeldOversikt": {"sumGjeld": 300000000000},
        },
    }
]


class TestRegnskapsClient(unittest.TestCase):
    """Test suite for RegnskapsClient endpoint querying and status classification."""

    def test_fetch_accounts_success(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_ACCOUNTS_PAYLOAD

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = RegnskapsClient(session=mock_session)
        res = client.fetch_accounts("923609016")

        self.assertEqual(res.status, ACCOUNTS_STATUS_SUCCESS)
        self.assertEqual(len(res.payload), 1)
        self.assertTrue(res.is_complete_set)

    def test_fetch_accounts_not_found(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = RegnskapsClient(session=mock_session)
        res = client.fetch_accounts("923609016")

        self.assertEqual(res.status, ACCOUNTS_STATUS_NOT_FOUND)
        self.assertIsNone(res.payload)
        self.assertTrue(res.is_complete_set)

    def test_fetch_accounts_identity_mismatch(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mismatched = [dict(SAMPLE_ACCOUNTS_PAYLOAD[0], virksomhet={"organisasjonsnummer": "999999999"})]
        mock_resp.json.return_value = mismatched

        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp

        client = RegnskapsClient(session=mock_session)
        res = client.fetch_accounts("923609016")

        self.assertEqual(res.status, ACCOUNTS_STATUS_FAILED)
        self.assertIn("Identity mismatch", res.error)

    def test_fetch_accounts_budget_exhaustion(self):
        conn = sqlite3.connect(":memory:")
        budget_tracker = RequestBudgetTracker(conn, budget_limit=0)

        client = RegnskapsClient(budget_tracker=budget_tracker)
        with self.assertRaises(RequestBudgetExceededError):
            client.fetch_accounts("923609016")


class TestExtractAccountFacts(unittest.TestCase):
    """Test suite for financial whitelist extraction and scope formatting."""

    def test_whitelist_extraction(self):
        facts = extract_account_facts_and_evidence(SAMPLE_ACCOUNTS_PAYLOAD, "923609016")
        fact_keys = [f[0] for f in facts]

        self.assertIn("accounts_2024_selskap_salgsinntekter", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_driftsinntekter", fact_keys)
        self.assertIn("accounts_2024_selskap_driftsresultat", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_driftskostnad", fact_keys)
        self.assertIn("accounts_2024_selskap_ordinaert_resultat_foer_skatt", fact_keys)
        self.assertIn("accounts_2024_selskap_aarsresultat", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_eiendeler", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_omloepsmidler", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_anleggsmidler", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_egenkapital", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_gjeld", fact_keys)

        # Ensure exact value breakdown
        salg_fact = next(f for f in facts if f[0] == "accounts_2024_selskap_salgsinntekter")
        val_dict = json.loads(salg_fact[1])
        self.assertEqual(val_dict["amount"], 350000000000)
        self.assertEqual(val_dict["currency"], "NOK")
        self.assertEqual(val_dict["scope"], "selskap")
        self.assertEqual(val_dict["year"], 2024)

    def test_zero_and_negative_values_preserved(self):
        payload = [
            {
                "regnskapstype": "KONSERN",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                "valuta": "NOK",
                "resultatregnskapResultat": {
                    "driftsresultat": {
                        "driftsinntekter": {"salgsinntekter": 0},
                        "driftsresultat": -50000,
                    }
                }
            }
        ]
        facts = extract_account_facts_and_evidence(payload, "923609016")
        val_zero = json.loads(next(f[1] for f in facts if f[0] == "accounts_2023_konsern_salgsinntekter"))
        val_neg = json.loads(next(f[1] for f in facts if f[0] == "accounts_2023_konsern_driftsresultat"))

        self.assertEqual(val_zero["amount"], 0)
        self.assertEqual(val_neg["amount"], -50000)
        self.assertEqual(val_neg["scope"], "konsern")


class TestStorageAndRunnerIntegration(unittest.TestCase):
    """Test persistence and runner isolation for financial facts."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_save_account_facts_storage(self):
        facts = extract_account_facts_and_evidence(SAMPLE_ACCOUNTS_PAYLOAD, "923609016")
        counts = save_account_facts_to_storage(self.conn, "923609016", facts)

        self.assertEqual(counts["created"], 11)
        db_facts = get_facts(self.conn, "923609016", active_only=True)
        self.assertEqual(len(db_facts), 11)

    def test_runner_fault_isolation(self):
        profile = NormalizedCompanyProfile(
            org_number="923609016",
            name="TEST EQUINOR ASA",
            business_address=Address(adresse=["Stavanger"], postnummer="4035", poststed="Stavanger"),
        )
        save_company_profile(self.conn, profile)

        runner = CompanyRunner(conn=self.conn)
        runner.accounts_client.fetch_accounts = MagicMock(side_effect=Exception("Regnskapsregisteret server timeout"))

        res = runner.process_company("923609016", live_refresh=True, include_roles=False, include_finanstilsynet=False, include_accounts=True)

        self.assertIn(res["status"], ("live_refreshed", "served_from_local", "local_fallback_after_refresh_failure"))
        self.assertEqual(res["source_status"]["accounts"], "failed")
        self.assertIn("Regnskapsregisteret API retrieval failed", res["warnings"][0])


if __name__ == "__main__":
    unittest.main()
