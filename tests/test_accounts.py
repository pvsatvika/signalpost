"""
Comprehensive unit test suite for Regnskapsregisteret financial safety, revision semantics,
and evidence context completeness.
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
    sanitize_numeric_amount,
)
from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.runner import CompanyRunner
from signal_post.storage import get_facts, init_db, save_company_profile
from signal_post.models import NormalizedCompanyProfile, Address


SAMPLE_ACCOUNTS_PAYLOAD = [
    {
        "id": 100,
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


class TestNumericSanitization(unittest.TestCase):
    """Test numeric precision, float-to-int conversion, and invalid type rejection."""

    def test_integers_and_floats(self):
        self.assertEqual(sanitize_numeric_amount(100), 100)
        self.assertEqual(sanitize_numeric_amount(100.0), 100)
        self.assertEqual(sanitize_numeric_amount(100.5), 100.5)

    def test_large_integers(self):
        large = 350000000000
        self.assertEqual(sanitize_numeric_amount(large), large)

    def test_zero_and_negative(self):
        self.assertEqual(sanitize_numeric_amount(0), 0)
        self.assertEqual(sanitize_numeric_amount(-50000), -50000)

    def test_booleans_and_invalid_rejected(self):
        self.assertIsNone(sanitize_numeric_amount(True))
        self.assertIsNone(sanitize_numeric_amount(False))
        self.assertIsNone(sanitize_numeric_amount(None))
        self.assertIsNone(sanitize_numeric_amount(""))
        self.assertIsNone(sanitize_numeric_amount("invalid"))
        self.assertIsNone(sanitize_numeric_amount(float("nan")))
        self.assertIsNone(sanitize_numeric_amount(float("inf")))


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
    """Test suite for financial whitelist extraction, scope formatting, and revision ordering."""

    def test_whitelist_extraction_and_evidence_context(self):
        facts = extract_account_facts_and_evidence(SAMPLE_ACCOUNTS_PAYLOAD, "923609016")
        fact_keys = [f[0] for f in facts]

        self.assertIn("accounts_2024_selskap_salgsinntekter", fact_keys)
        self.assertIn("accounts_2024_selskap_sum_driftsinntekter", fact_keys)

        # Verify 100% evidence context completeness
        salg_fact = next(f for f in facts if f[0] == "accounts_2024_selskap_salgsinntekter")
        val_dict = json.loads(salg_fact[1])

        self.assertEqual(val_dict["amount"], 350000000000)
        self.assertEqual(val_dict["currency"], "NOK")
        self.assertEqual(val_dict["org_number"], "923609016")
        self.assertEqual(val_dict["period_from"], "2024-01-01")
        self.assertEqual(val_dict["period_to"], "2024-12-31")
        self.assertEqual(val_dict["regnskapstype"], "SELSKAP")
        self.assertEqual(val_dict["scope"], "selskap")
        self.assertEqual(val_dict["year"], 2024)
        self.assertEqual(val_dict["field_name"], "salgsinntekter")

    def test_currency_preservation_usd(self):
        payload = [
            {
                "id": 200,
                "journalnr": "2024515327",
                "regnskapstype": "KONSERN",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"},
                "valuta": "USD",
                "resultatregnskapResultat": {
                    "driftsresultat": {
                        "driftsinntekter": {"salgsinntekter": 106848000000},
                    }
                }
            }
        ]
        facts = extract_account_facts_and_evidence(payload, "923609016")
        val_dict = json.loads(next(f[1] for f in facts if f[0] == "accounts_2023_konsern_salgsinntekter"))

        self.assertEqual(val_dict["currency"], "USD")
        self.assertEqual(val_dict["amount"], 106848000000)
        self.assertEqual(val_dict["scope"], "konsern")

    def test_non_calendar_period_collision_prevention(self):
        payload = [
            {
                "id": 1,
                "journalnr": "2024010100",
                "regnskapstype": "SELSKAP",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2023-04-01", "tilDato": "2024-03-31"},
                "valuta": "NOK",
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"salgsinntekter": 1000000}}
                }
            }
        ]
        facts = extract_account_facts_and_evidence(payload, "923609016")
        fact_key = facts[0][0]

        # Key must be qualified with dates for non-calendar period to prevent collision
        self.assertEqual(fact_key, "accounts_2024_20230401_20240331_selskap_salgsinntekter")

    def test_company_vs_consolidated_scope_separation(self):
        payload = [
            {
                "id": 1,
                "regnskapstype": "SELSKAP",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
                "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"salgsinntekter": 50000}}}
            },
            {
                "id": 2,
                "regnskapstype": "KONSERN",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
                "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"salgsinntekter": 500000}}}
            }
        ]
        facts = extract_account_facts_and_evidence(payload, "923609016")
        fact_dict = {f[0]: json.loads(f[1])["amount"] for f in facts}

        self.assertEqual(fact_dict["accounts_2024_selskap_salgsinntekter"], 50000)
        self.assertEqual(fact_dict["accounts_2024_konsern_salgsinntekter"], 500000)

    def test_deterministic_revision_ordering_normal_and_reversed(self):
        old_filing = {
            "id": 10,
            "journalnr": "2024100100",
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"salgsinntekter": 100}}}
        }
        revised_filing = {
            "id": 20,
            "journalnr": "2024100200",  # Higher journal number = latest revision
            "regnskapstype": "SELSKAP",
            "virksomhet": {"organisasjonsnummer": "923609016"},
            "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
            "resultatregnskapResultat": {"driftsresultat": {"driftsinntekter": {"salgsinntekter": 200}}}
        }

        # Test normal order [old, revised]
        facts_normal = extract_account_facts_and_evidence([old_filing, revised_filing], "923609016")
        val_normal = json.loads(facts_normal[0][1])["amount"]

        # Test reversed order [revised, old]
        facts_reversed = extract_account_facts_and_evidence([revised_filing, old_filing], "923609016")
        val_reversed = json.loads(facts_reversed[0][1])["amount"]

        # Both must deterministically output the latest revision amount (200)
        self.assertEqual(val_normal, 200)
        self.assertEqual(val_reversed, 200)

    def test_unknown_future_fields_ignored(self):
        payload_with_unknown = [
            {
                "id": 1,
                "regnskapstype": "SELSKAP",
                "virksomhet": {"organisasjonsnummer": "923609016"},
                "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
                "futureCryptoAssets": 999999,
                "unknownField": "should_be_ignored",
                "resultatregnskapResultat": {
                    "driftsresultat": {"driftsinntekter": {"salgsinntekter": 500}}
                }
            }
        ]
        facts = extract_account_facts_and_evidence(payload_with_unknown, "923609016")

        # Must only extract whitelisted 'salgsinntekter'
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0][0], "accounts_2024_selskap_salgsinntekter")


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
