"""
Brønnøysund Registry HTTP Client.
"""

import re
from typing import Any, Dict, Optional, TYPE_CHECKING
import requests

from signal_post.exceptions import (
    InvalidOrgNumberError,
    CompanyNotFoundError,
    RegistryClientError,
    DataMismatchError,
)
from signal_post.models import NormalizedCompanyProfile

if TYPE_CHECKING:
    from signal_post.budget import RequestBudgetTracker

BRREG_ENHETER_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}"
ORG_NUMBER_REGEX = re.compile(r"^\d{9}$")


class BrregClient:
    """Client for interacting with the Norwegian Brønnøysund Registry API."""

    def __init__(
        self,
        timeout: float = 10.0,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[Any] = None,
    ):
        """
        Initialize the BrregClient.

        :param timeout: HTTP request timeout in seconds.
        :param session: Optional custom requests.Session for connection reuse or testing.
        :param budget_tracker: Optional RequestBudgetTracker instance for request budget enforcement.
        """
        self.timeout = timeout
        self.session = session or requests.Session()
        self.budget_tracker = budget_tracker

    @staticmethod
    def validate_org_number(org_number: str) -> str:
        """
        Validate that input is a valid 9-digit organization number.

        :param org_number: The input organization number.
        :return: Cleaned 9-digit organization string.
        :raises InvalidOrgNumberError: If input is not a 9-digit string.
        """
        if not isinstance(org_number, str):
            org_number = str(org_number)
        cleaned = org_number.strip()
        if not ORG_NUMBER_REGEX.match(cleaned):
            raise InvalidOrgNumberError(
                f"Invalid organization number format: '{org_number}'. Must be exactly 9 digits."
            )
        return cleaned

    def fetch_raw_company(self, org_number: str) -> Dict[str, Any]:
        """
        Fetch raw JSON record for a company from the official Brønnøysund Registry API.

        :param org_number: Norwegian organization number.
        :return: Raw dictionary parsed from registry JSON.
        :raises InvalidOrgNumberError: If org_number format is invalid.
        :raises CompanyNotFoundError: If 404 Not Found returned.
        :raises DataMismatchError: If returned org number does not match requested.
        :raises RegistryClientError: For network errors, timeouts, HTTP non-200, or invalid JSON.
        """
        valid_org_num = self.validate_org_number(org_number)
        url = BRREG_ENHETER_URL.format(org_number=valid_org_num)
        headers = {"Accept": "application/json"}

        if self.budget_tracker:
            self.budget_tracker.record_request(request_type="company_fetch", target_url=url)

        try:
            response = self.session.get(url, headers=headers, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            raise RegistryClientError(
                f"Request timed out connecting to Brønnøysund Registry for org number '{valid_org_num}': {e}"
            ) from e
        except requests.exceptions.ConnectionError as e:
            raise RegistryClientError(
                f"Network connection failure contacting Brønnøysund Registry: {e}"
            ) from e
        except requests.exceptions.RequestException as e:
            raise RegistryClientError(f"HTTP request error: {e}") from e

        if response.status_code == 404:
            raise CompanyNotFoundError(
                f"Company with organization number '{valid_org_num}' not found in Brønnøysund Registry."
            )
        elif response.status_code != 200:
            raise RegistryClientError(
                f"Registry returned HTTP error {response.status_code} for org number '{valid_org_num}'."
            )

        try:
            data = response.json()
        except Exception as e:
            raise RegistryClientError(
                f"Invalid JSON response received from registry for org number '{valid_org_num}': {e}"
            ) from e

        if not isinstance(data, dict):
            raise RegistryClientError(
                f"Malformed response format: expected JSON object, got {type(data).__name__}."
            )

        returned_org = str(data.get("organisasjonsnummer", ""))
        if returned_org != valid_org_num:
            raise DataMismatchError(
                f"Organization number mismatch: requested '{valid_org_num}', but registry returned '{returned_org}'."
            )

        return data

    def get_company_profile(self, org_number: str) -> NormalizedCompanyProfile:
        """
        Fetch company record and return a normalized company profile.

        :param org_number: Norwegian organization number.
        :return: NormalizedCompanyProfile instance.
        """
        raw_data = self.fetch_raw_company(org_number)
        return NormalizedCompanyProfile.from_raw_dict(raw_data)
