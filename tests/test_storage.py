"""
Unit tests for SQLite storage module, facts, evidence, change history, and database integrity.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from signal_post.models import NormalizedCompanyProfile, Address, OrganizationForm
from signal_post.storage import (
    get_connection,
    init_db,
    save_company_profile,
    get_company,
    get_facts,
    get_evidence_for_fact,
    get_change_history,
    get_company_profile_with_evidence,
    extract_facts,
)
from signal_post.exceptions import InvalidOrgNumberError


class TestStorage(unittest.TestCase):

    def setUp(self):
        # Use temporary file database for disk-persistence and transaction isolation testing
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "test_signalpost.db")
        self.conn = get_connection(self.db_path)
        init_db(self.conn)

        self.sample_raw_1 = {
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 487,
            "organisasjonsform": {"kode": "ORGL", "beskrivelse": "Organisasjonsledd"},
            "hjemmeside": "www.brreg.no",
            "registreringsdatoEnhetsregisteret": "1995-08-09",
            "registrertIForetaksregisteret": False,
        }
        self.profile_1 = NormalizedCompanyProfile.from_raw_dict(self.sample_raw_1)

    def tearDown(self):
        self.conn.close()
        self.temp_dir.cleanup()

    def test_database_initialization(self):
        """Test that init_db creates all required tables, indexes, and seed source records."""
        cur = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = {row["name"] for row in cur.fetchall()}
        expected_tables = {"companies", "sources", "facts", "evidence", "change_history"}
        self.assertTrue(expected_tables.issubset(tables))

        # Check default source record
        cur = self.conn.execute("SELECT * FROM sources WHERE source_id = 'brreg_enhetsregisteret';")
        source = cur.fetchone()
        self.assertIsNotNone(source)
        self.assertEqual(source["name"], "Brønnøysundregistrene - Enhetsregisteret")

    def test_save_and_retrieve_company(self):
        """Test saving a normalized company profile and retrieving company & facts."""
        save_company_profile(self.conn, self.profile_1)

        comp = get_company(self.conn, "974760673")
        self.assertIsNotNone(comp)
        self.assertEqual(comp["name"], "REGISTERENHETEN I BRØNNØYSUND")
        self.assertEqual(comp["organization_form_code"], "ORGL")
        self.assertEqual(comp["num_employees"], 487)

        facts = get_facts(self.conn, "974760673", active_only=True)
        fact_keys = {f["fact_key"] for f in facts}
        self.assertIn("name", fact_keys)
        self.assertIn("num_employees", fact_keys)
        self.assertIn("website", fact_keys)
        self.assertIn("organization_form", fact_keys)

        # Verification status for single source should be source_asserted
        name_fact = [f for f in facts if f["fact_key"] == "name"][0]
        self.assertEqual(name_fact["verification_status"], "source_asserted")

    def test_saving_facts_and_evidence(self):
        """Test that every saved fact has attached evidence with raw payload and URL."""
        save_company_profile(self.conn, self.profile_1)

        profile_data = get_company_profile_with_evidence(self.conn, "974760673")
        self.assertIsNotNone(profile_data)
        facts = profile_data["facts"]
        self.assertTrue(len(facts) > 0)

        for fact in facts:
            evidence_list = fact["evidence"]
            self.assertTrue(len(evidence_list) >= 1)
            ev = evidence_list[0]
            self.assertEqual(ev["source_id"], "brreg_enhetsregisteret")
            self.assertIn("974760673", ev["exact_source_url"])
            self.assertIn("REGISTERENHETEN I BRØNNØYSUND", ev["raw_evidence"])

    def test_missing_optional_values_not_stored_as_assertions(self):
        """Test that missing optional attributes (None) are not stored as empty facts."""
        raw_minimal = {
            "organisasjonsnummer": "123456789",
            "navn": "Minimal AS"
        }
        profile_min = NormalizedCompanyProfile.from_raw_dict(raw_minimal)
        save_company_profile(self.conn, profile_min)

        facts = get_facts(self.conn, "123456789", active_only=True)
        fact_keys = {f["fact_key"] for f in facts}

        self.assertIn("name", fact_keys)
        self.assertNotIn("num_employees", fact_keys)
        self.assertNotIn("website", fact_keys)
        self.assertNotIn("is_bankrupt", fact_keys)
        self.assertNotIn("under_liquidation", fact_keys)

    def test_repeated_unchanged_facts_no_duplicate_history(self):
        """Test that saving the exact same profile twice updates observed time but does not duplicate history."""
        save_company_profile(self.conn, self.profile_1, retrieved_at="2026-10-04T10:00:00Z")
        initial_history = get_change_history(self.conn, "974760673")
        initial_history_count = len(initial_history)

        # Save again with unchanged data from same source
        save_company_profile(self.conn, self.profile_1, retrieved_at="2026-10-04T12:00:00Z")
        second_history = get_change_history(self.conn, "974760673")

        # History count must remain identical
        self.assertEqual(len(second_history), initial_history_count)

        # Multiple evidence records should now exist for the facts
        facts = get_facts(self.conn, "974760673", active_only=True)
        name_fact = [f for f in facts if f["fact_key"] == "name"][0]
        evidence = get_evidence_for_fact(self.conn, name_fact["fact_id"])
        self.assertEqual(len(evidence), 2)

    def test_same_source_chronological_update(self):
        """Test that updating a fact from the SAME source deactivates old fact and creates new active fact."""
        save_company_profile(self.conn, self.profile_1, source_id="brreg_enhetsregisteret", retrieved_at="2026-10-04T10:00:00Z")

        # Create updated profile from same source with new employee count
        updated_raw = dict(self.sample_raw_1)
        updated_raw["antallAnsatte"] = 500
        updated_profile = NormalizedCompanyProfile.from_raw_dict(updated_raw)

        save_company_profile(self.conn, updated_profile, source_id="brreg_enhetsregisteret", retrieved_at="2026-10-04T15:00:00Z")

        # Active fact should have value 500
        active_facts = get_facts(self.conn, "974760673", active_only=True)
        emp_fact = [f for f in active_facts if f["fact_key"] == "num_employees"][0]
        self.assertEqual(emp_fact["fact_value"], "500")

        # All facts (including inactive) should include old fact
        all_facts = get_facts(self.conn, "974760673", active_only=False)
        emp_facts = [f for f in all_facts if f["fact_key"] == "num_employees"]
        self.assertEqual(len(emp_facts), 2)

        # Check change history
        history = get_change_history(self.conn, "974760673")
        emp_changes = [h for h in history if h["fact_key"] == "num_employees"]
        self.assertEqual(len(emp_changes), 2)
        self.assertEqual(emp_changes[0]["change_type"], "created")
        self.assertEqual(emp_changes[1]["change_type"], "updated")
        self.assertEqual(emp_changes[1]["previous_value"], "487")
        self.assertEqual(emp_changes[1]["new_value"], "500")

    def test_multi_source_corroboration(self):
        """Test that a second independent source asserting the SAME value upgrades status to 'corroborated'."""
        save_company_profile(self.conn, self.profile_1, source_id="brreg_enhetsregisteret", retrieved_at="2026-10-04T10:00:00Z")

        # Save exact same profile from a DIFFERENT source
        save_company_profile(self.conn, self.profile_1, source_id="secondary_registry", retrieved_at="2026-10-04T12:00:00Z")

        facts = get_facts(self.conn, "974760673", active_only=True)
        name_fact = [f for f in facts if f["fact_key"] == "name"][0]

        self.assertEqual(name_fact["verification_status"], "corroborated")
        evidence = get_evidence_for_fact(self.conn, name_fact["fact_id"])
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["source_id"], "brreg_enhetsregisteret")
        self.assertEqual(evidence[1]["source_id"], "secondary_registry")

    def test_multi_source_conflict_handling(self):
        """Test that a DIFFERENT source asserting a CONFLICTING value flags status as 'conflicting' without overwriting."""
        save_company_profile(self.conn, self.profile_1, source_id="brreg_enhetsregisteret", retrieved_at="2026-10-04T10:00:00Z")

        # Create profile with conflicting employee count from DIFFERENT source
        conflicting_raw = dict(self.sample_raw_1)
        conflicting_raw["antallAnsatte"] = 999
        conflicting_profile = NormalizedCompanyProfile.from_raw_dict(conflicting_raw)

        save_company_profile(self.conn, conflicting_profile, source_id="third_party_vendor", retrieved_at="2026-10-04T12:00:00Z")

        facts = get_facts(self.conn, "974760673", active_only=True)
        emp_fact = [f for f in facts if f["fact_key"] == "num_employees"][0]

        # Verification status must be flagged as conflicting
        self.assertEqual(emp_fact["verification_status"], "conflicting")
        # Fact value is preserved as original authoritative value 487 (not overwritten silently)
        self.assertEqual(emp_fact["fact_value"], "487")

        # Evidence must contain both sources
        evidence = get_evidence_for_fact(self.conn, emp_fact["fact_id"])
        self.assertEqual(len(evidence), 2)
        self.assertEqual(evidence[0]["source_id"], "brreg_enhetsregisteret")
        self.assertEqual(evidence[1]["source_id"], "third_party_vendor")

        # Change history must log a 'conflict' record
        history = get_change_history(self.conn, "974760673")
        conflict_history = [h for h in history if h["change_type"] == "conflict"]
        self.assertEqual(len(conflict_history), 1)
        self.assertIn("Conflict: source 'third_party_vendor'", conflict_history[0]["reason"])

    def test_invalid_org_number_rejected_in_storage(self):
        """Test that storage operations reject invalid org numbers."""
        invalid_raw = {
            "organisasjonsnummer": "123",
            "navn": "Bad Org AS"
        }
        with self.assertRaises(InvalidOrgNumberError):
            invalid_profile = NormalizedCompanyProfile.from_raw_dict(invalid_raw)
            save_company_profile(self.conn, invalid_profile)

    def test_transaction_rollback_on_failure(self):
        """Test that an error inside transaction rolls back all partial writes."""
        try:
            with self.conn:
                self.conn.execute("INSERT INTO companies (org_number, name, first_seen_at, last_checked_at) VALUES ('999999999', 'Test', 'now', 'now');")
                # Force constraint failure
                self.conn.execute("INSERT INTO companies (org_number, name, first_seen_at, last_checked_at) VALUES ('999999999', 'Duplicate', 'now', 'now');")
        except sqlite3.IntegrityError:
            pass

        # Company '999999999' should not exist due to rollback
        comp = get_company(self.conn, "999999999")
        self.assertIsNone(comp)

    def test_foreign_key_cascade_deletion(self):
        """Test foreign key behavior: deleting a company cascades to facts, evidence, and change history."""
        save_company_profile(self.conn, self.profile_1)

        with self.conn:
            self.conn.execute("DELETE FROM companies WHERE org_number = '974760673';")

        facts = get_facts(self.conn, "974760673", active_only=False)
        history = get_change_history(self.conn, "974760673")
        self.assertEqual(len(facts), 0)
        self.assertEqual(len(history), 0)

        cur = self.conn.execute("SELECT * FROM evidence;")
        evidence = cur.fetchall()
        self.assertEqual(len(evidence), 0)


if __name__ == "__main__":
    unittest.main()
