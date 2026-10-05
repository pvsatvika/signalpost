"""
Unit tests for Pydantic models in Signalpost.
"""

import unittest
from signal_post.models import (
    NormalizedCompanyProfile,
    Address,
    IndustryCode,
    OrganizationForm,
    CapitalInfo,
    HistoricalName,
)


class TestModels(unittest.TestCase):

    def test_address_model_optional_fields(self):
        """Test Address model handles missing optional fields gracefully."""
        addr = Address(adresse=["Storgata 1"])
        self.assertEqual(addr.adresse, ["Storgata 1"])
        self.assertIsNone(addr.postnummer)
        self.assertIsNone(addr.poststed)

    def test_minimal_company_profile(self):
        """Test NormalizedCompanyProfile with minimal required fields."""
        raw = {
            "organisasjonsnummer": "123456789",
            "navn": "Test Company AS"
        }
        profile = NormalizedCompanyProfile.from_raw_dict(raw)
        self.assertEqual(profile.org_number, "123456789")
        self.assertEqual(profile.name, "Test Company AS")
        self.assertIsNone(profile.organization_form)
        self.assertIsNone(profile.primary_industry)
        self.assertIsNone(profile.num_employees)
        self.assertEqual(profile.raw_data, raw)

    def test_unspecified_booleans_remain_none(self):
        """Test that missing boolean fields (e.g. bankrupt, liquidation) return None rather than assuming False."""
        raw = {
            "organisasjonsnummer": "123456789",
            "navn": "Test Company AS"
        }
        profile = NormalizedCompanyProfile.from_raw_dict(raw)
        self.assertIsNone(profile.is_bankrupt)
        self.assertIsNone(profile.under_liquidation)
        self.assertIsNone(profile.under_compulsory_dissolution)

    def test_raw_data_copy_isolation(self):
        """Test that mutating original dict does not alter profile.raw_data."""
        raw = {
            "organisasjonsnummer": "123456789",
            "navn": "Test Company AS"
        }
        profile = NormalizedCompanyProfile.from_raw_dict(raw)
        raw["navn"] = "Mutated Name"
        self.assertEqual(profile.raw_data["navn"], "Test Company AS")

    def test_full_company_profile_parsing(self):
        """Test parsing of complete company response object with extended fields."""
        raw = {
            "organisasjonsnummer": "923609016",
            "navn": "EQUINOR ASA",
            "antallAnsatte": 21272,
            "harRegistrertAntallAnsatte": True,
            "overordnetEnhet": "912660680",
            "stiftelsesdato": "1972-09-18",
            "organisasjonsform": {
                "kode": "ASA",
                "beskrivelse": "Allmennaksjeselskap"
            },
            "naeringskode1": {
                "kode": "06.100",
                "beskrivelse": "Utvinning av råolje"
            },
            "naeringskode2": {
                "kode": "06.200",
                "beskrivelse": "Utvinning av naturgass"
            },
            "historiskeNavn": [
                {
                    "fraDato": "2001-05-11 08:32:09",
                    "navn": "STATOIL ASA",
                    "tilDato": "2007-10-01 06:44:33"
                }
            ],
            "kapital": {
                "belop": 5976872600.0,
                "valuta": "NOK",
                "antallAksjer": 2390749040,
                "type": "Aksjekapital"
            },
            "forretningsadresse": {
                "adresse": ["Forusbeen 50"],
                "postnummer": "4035",
                "poststed": "STAVANGER",
                "land": "Norge"
            },
            "hjemmeside": "www.equinor.com",
            "registrertIForetaksregisteret": True,
            "registrertIMvaregisteret": True,
            "erIKonsern": True,
            "konkurs": False,
            "underAvvikling": False,
            "underTvangsavviklingEllerTvangsopplosning": False,
            "sisteInnsendteAarsregnskap": "2025"
        }

        profile = NormalizedCompanyProfile.from_raw_dict(raw)

        self.assertEqual(profile.org_number, "923609016")
        self.assertEqual(profile.name, "EQUINOR ASA")
        self.assertEqual(profile.parent_org_number, "912660680")
        self.assertEqual(profile.foundation_date, "1972-09-18")
        self.assertEqual(profile.num_employees, 21272)
        self.assertTrue(profile.has_registered_employees)
        self.assertIsNotNone(profile.organization_form)
        self.assertEqual(profile.organization_form.kode, "ASA")
        self.assertEqual(profile.primary_industry.kode, "06.100")
        self.assertEqual(profile.secondary_industry.kode, "06.200")
        self.assertEqual(len(profile.historical_names), 1)
        self.assertEqual(profile.historical_names[0].navn, "STATOIL ASA")
        self.assertIsNotNone(profile.capital)
        self.assertEqual(profile.capital.belop, 5976872600.0)
        self.assertEqual(profile.business_address.poststed, "STAVANGER")
        self.assertEqual(profile.website, "www.equinor.com")
        self.assertTrue(profile.registered_in_foretaksregisteret)
        self.assertTrue(profile.registered_in_mvaregisteret)
        self.assertTrue(profile.is_in_group)
        self.assertFalse(profile.is_bankrupt)
        self.assertFalse(profile.under_liquidation)
        self.assertEqual(profile.last_submitted_annual_accounts, "2025")
        self.assertEqual(profile.raw_data, raw)


if __name__ == "__main__":
    unittest.main()
