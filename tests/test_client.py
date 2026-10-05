"""
Unit tests for BrregClient using mocked HTTP responses.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock
import requests

from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError
from signal_post.client import BrregClient
from signal_post.exceptions import (
    InvalidOrgNumberError,
    CompanyNotFoundError,
    RegistryClientError,
    DataMismatchError,
)
from signal_post.models import NormalizedCompanyProfile
from signal_post.storage import get_connection, init_db


class TestBrregClient(unittest.TestCase):

    def setUp(self):
        self.mock_session = MagicMock(spec=requests.Session)
        self.client = BrregClient(session=self.mock_session)

        # Sample JSON response matching real Brønnøysund API output
        self.sample_raw_data = {
            "organisasjonsnummer": "974760673",
            "navn": "REGISTERENHETEN I BRØNNØYSUND",
            "antallAnsatte": 487,
            "harRegistrertAntallAnsatte": True,
            "epostadresse": "firmapost@brreg.no",
            "hjemmeside": "www.brreg.no",
            "telefon": "75 00 75 09",
            "organisasjonsform": {
                "kode": "ORGL",
                "beskrivelse": "Organisasjonsledd"
            },
            "naeringskode1": {
                "kode": "84.110",
                "beskrivelse": "Generell offentlig administrasjon"
            },
            "forretningsadresse": {
                "adresse": ["Havnegata 48"],
                "postnummer": "8900",
                "poststed": "BRØNNØYSUND",
                "kommune": "BRØNNØY",
                "land": "Norge",
                "landkode": "NO"
            },
            "postadresse": {
                "adresse": ["Postboks 900"],
                "postnummer": "8910",
                "poststed": "BRØNNØYSUND",
                "kommune": "BRØNNØY",
                "land": "Norge"
            },
            "registreringsdatoEnhetsregisteret": "1995-08-09",
            "registrertIForetaksregisteret": False,
            "registrertIMvaregisteret": False,
            "underAvvikling": False,
            "underTvangsavviklingEllerTvangsopplosning": False,
            "konkurs": False
        }

    def _create_mock_response(self, status_code=200, json_data=None, text_data=None):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        if json_data is not None:
            mock_resp.json.return_value = json_data
        if text_data is not None:
            mock_resp.text = text_data
            mock_resp.json.side_effect = ValueError("Expecting value: line 1 column 1 (char 0)")
        return mock_resp

    def test_valid_org_number_success(self):
        """Test retrieving a valid company profile with mocked 200 response."""
        self.mock_session.get.return_value = self._create_mock_response(
            status_code=200, json_data=self.sample_raw_data
        )

        profile = self.client.get_company_profile("974760673")

        self.assertIsInstance(profile, NormalizedCompanyProfile)
        self.assertEqual(profile.org_number, "974760673")
        self.assertEqual(profile.name, "REGISTERENHETEN I BRØNNØYSUND")
        self.assertEqual(profile.num_employees, 487)
        self.assertEqual(profile.organization_form.kode, "ORGL")
        self.assertEqual(profile.primary_industry.kode, "84.110")
        self.assertEqual(profile.business_address.postnummer, "8900")
        self.assertEqual(profile.raw_data, self.sample_raw_data)
        
        self.mock_session.get.assert_called_once_with(
            "https://data.brreg.no/enhetsregisteret/api/enheter/974760673",
            headers={"Accept": "application/json"},
            timeout=10.0,
        )

    def test_valid_org_number_with_whitespace(self):
        """Test that whitespace around org number is trimmed and validated."""
        self.mock_session.get.return_value = self._create_mock_response(
            status_code=200, json_data=self.sample_raw_data
        )

        profile = self.client.get_company_profile("  974760673 \n")
        self.assertEqual(profile.org_number, "974760673")

    def test_invalid_org_number_format_short(self):
        """Test invalid org number (too short) raises InvalidOrgNumberError."""
        with self.assertRaises(InvalidOrgNumberError):
            self.client.get_company_profile("12345678")
        self.mock_session.get.assert_not_called()

    def test_invalid_org_number_format_long(self):
        """Test invalid org number (too long) raises InvalidOrgNumberError."""
        with self.assertRaises(InvalidOrgNumberError):
            self.client.get_company_profile("1234567890")
        self.mock_session.get.assert_not_called()

    def test_invalid_org_number_format_non_numeric(self):
        """Test invalid org number (non-numeric characters) raises InvalidOrgNumberError."""
        with self.assertRaises(InvalidOrgNumberError):
            self.client.get_company_profile("97476067A")
        self.mock_session.get.assert_not_called()

    def test_missing_record_404(self):
        """Test 404 response raises CompanyNotFoundError."""
        self.mock_session.get.return_value = self._create_mock_response(status_code=404)

        with self.assertRaises(CompanyNotFoundError) as ctx:
            self.client.get_company_profile("999999999")
        self.assertIn("not found in Brønnøysund Registry", str(ctx.exception))

    def test_company_number_mismatch(self):
        """Test response with mismatched org number raises DataMismatchError."""
        mismatched_data = dict(self.sample_raw_data)
        mismatched_data["organisasjonsnummer"] = "111222333"

        self.mock_session.get.return_value = self._create_mock_response(
            status_code=200, json_data=mismatched_data
        )

        with self.assertRaises(DataMismatchError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("mismatch", str(ctx.exception))

    def test_malformed_json_response(self):
        """Test response with invalid JSON string raises RegistryClientError."""
        self.mock_session.get.return_value = self._create_mock_response(
            status_code=200, text_data="<html>502 Bad Gateway</html>"
        )

        with self.assertRaises(RegistryClientError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("Invalid JSON", str(ctx.exception))

    def test_non_dict_json_response(self):
        """Test response with root JSON array instead of object raises RegistryClientError."""
        self.mock_session.get.return_value = self._create_mock_response(
            status_code=200, json_data=["item1", "item2"]
        )

        with self.assertRaises(RegistryClientError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("Malformed response format", str(ctx.exception))

    def test_http_500_server_error(self):
        """Test HTTP 500 error raises RegistryClientError."""
        self.mock_session.get.return_value = self._create_mock_response(status_code=500)

        with self.assertRaises(RegistryClientError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("HTTP error 500", str(ctx.exception))

    def test_http_timeout(self):
        """Test HTTP request timeout raises RegistryClientError."""
        self.mock_session.get.side_effect = requests.exceptions.Timeout("Connection timed out")

        with self.assertRaises(RegistryClientError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("timed out", str(ctx.exception))

    def test_connection_failure(self):
        """Test network connection error raises RegistryClientError."""
        self.mock_session.get.side_effect = requests.exceptions.ConnectionError("Failed to resolve host")

        with self.assertRaises(RegistryClientError) as ctx:
            self.client.get_company_profile("974760673")
        self.assertIn("connection failure", str(ctx.exception).lower())

    def test_client_request_budget_enforcement(self):
        """Test BrregClient enforces RequestBudgetTracker before HTTP calls."""
        temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        try:
            db_path = str(Path(temp_dir.name) / "test_client_budget.db")
            conn = get_connection(db_path)
            init_db(conn)
            tracker = RequestBudgetTracker(conn, budget_limit=1)

            client = BrregClient(session=self.mock_session, budget_tracker=tracker)
            self.mock_session.get.return_value = self._create_mock_response(200, json_data={"organisasjonsnummer": "974760673"})

            # First call succeeds and consumes 1 request
            client.fetch_raw_company("974760673")
            self.assertEqual(tracker.count_requests_today(), 1)

            # Second call raises RequestBudgetExceededError before calling session.get
            with self.assertRaises(RequestBudgetExceededError):
                client.fetch_raw_company("974760673")
            conn.close()
        finally:
            temp_dir.cleanup()


if __name__ == "__main__":
    unittest.main()
