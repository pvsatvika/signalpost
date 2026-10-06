"""
Official Brønnøysund Regnskapsregisteret REST API integration for Signalpost.

Endpoint (open, public, no authentication required):
    GET https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}

Responsibilities:
- Budget-enforced, identity-verified retrieval of structured annual financial key figures.
- Strict whitelist of official source fields only (no derived financial ratios, margins, or formulas).
- Separate company ('SELSKAP') vs consolidated group ('KONSERN') accounts.
- Period-specific, scope-aware fact keys: accounts_<period_key>_<scope>_<field_name>.
- Deterministic revision ordering: chooses the latest resubmitted statement based on journalnr/id regardless of API array order.
- Conservative status semantics: distinguish 'success' from 'success_empty', 'not_found', 'failed', 'malformed', 'budget_exhausted'.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union
import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.storage import init_db

logger = logging.getLogger(__name__)

ACCOUNTS_SOURCE_ID = "brreg_accounts_key_figures"
ACCOUNTS_SOURCE_NAME = "Brønnøysund Register Centre - Regnskapsregisteret"
ACCOUNTS_URL_TEMPLATE = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"

# Outcome status categories
ACCOUNTS_STATUS_SUCCESS = "success"                # Valid accounting payload with >=1 extracted field
ACCOUNTS_STATUS_EMPTY = "success_empty"            # Response received but no financial statements or whitelisted fields
ACCOUNTS_STATUS_NOT_FOUND = "not_found"            # HTTP 404 (no accounts registered or not required to submit)
ACCOUNTS_STATUS_FAILED = "failed"                  # Network error, timeout, HTTP error, or identity mismatch
ACCOUNTS_STATUS_MALFORMED = "malformed"            # Unexpected schema or JSON decode failure
ACCOUNTS_STATUS_BUDGET = "budget_exhausted"        # Outbound request blocked before transmission

# Strict whitelist of approved source paths
# Path tuple -> field_name suffix
WHITELISTED_FINANCIAL_FIELDS = [
    (("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "salgsinntekter"), "salgsinntekter"),
    (("resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"), "sum_driftsinntekter"),
    (("resultatregnskapResultat", "driftsresultat", "driftsresultat"), "driftsresultat"),
    (("resultatregnskapResultat", "driftsresultat", "driftskostnad", "sumDriftskostnad"), "sum_driftskostnad"),
    (("resultatregnskapResultat", "ordinaertResultatFoerSkattekostnad"), "ordinaert_resultat_foer_skatt"),
    (("resultatregnskapResultat", "aarsresultat"), "aarsresultat"),
    (("eiendeler", "sumEiendeler"), "sum_eiendeler"),
    (("eiendeler", "omloepsmidler", "sumOmloepsmidler"), "sum_omloepsmidler"),
    (("eiendeler", "anleggsmidler", "sumAnleggsmidler"), "sum_anleggsmidler"),
    (("egenkapitalGjeld", "egenkapital", "sumEgenkapital"), "sum_egenkapital"),
    (("egenkapitalGjeld", "gjeldOversikt", "sumGjeld"), "sum_gjeld"),
]


def _get_nested(d: Any, path: Tuple[str, ...]) -> Any:
    curr = d
    for key in path:
        if isinstance(curr, dict):
            curr = curr.get(key)
        else:
            return None
    return curr


def sanitize_numeric_amount(val: Any) -> Optional[Union[int, float]]:
    """
    Sanitize and validate numeric financial amounts.
    - Preserves exact Python integers (no float rounding or scientific notation).
    - Converts float values with zero fractional part to int.
    - Preserves exact non-zero decimals as float.
    - Rejects bool, None, NaN, Infinity, and non-numeric strings.
    - Preserves exact zero (0) and negative values.
    """
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            return None
        return int(val) if val.is_integer() else val
    if isinstance(val, str):
        val_str = val.strip()
        if not val_str:
            return None
        try:
            num = float(val_str)
            if math.isnan(num) or math.isinf(num):
                return None
            return int(num) if num.is_integer() else num
        except (ValueError, OverflowError):
            return None
    return None


@dataclass
class AccountsFetchResult:
    status: str
    http_status: Optional[int] = None
    payload: Optional[List[Dict[str, Any]]] = None  # List of raw account objects
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_set(self) -> bool:
        """True only for identity-verified successful responses (safe basis for removal detection)."""
        return self.status in (ACCOUNTS_STATUS_SUCCESS, ACCOUNTS_STATUS_EMPTY, ACCOUNTS_STATUS_NOT_FOUND)


class RegnskapsClient:
    """Budget-enforced client for the official open Brønnøysund Regnskapsregisteret API."""

    def __init__(
        self,
        client: Optional[BrregClient] = None,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        timeout: float = 10.0,
    ):
        self.client = client
        self.session = session or (client.session if client else requests.Session())
        self.budget_tracker = budget_tracker or (client.budget_tracker if client else None)
        self.timeout = timeout or (client.timeout if client else 10.0)

    def fetch_accounts(self, org_number: str) -> AccountsFetchResult:
        """
        Retrieve and classify financial accounts for a 9-digit Norwegian organization number.
        Returns AccountsFetchResult. Never raises for network/HTTP errors.
        Raises RequestBudgetExceededError if budget limit is reached before transmission.
        """
        valid_org = BrregClient.validate_org_number(org_number)
        url = ACCOUNTS_URL_TEMPLATE.format(org=valid_org)

        if self.budget_tracker:
            self.budget_tracker.record_request("accounts_fetch", url)

        result = AccountsFetchResult(status=ACCOUNTS_STATUS_FAILED, source_url=url)
        try:
            resp = self.session.get(url, headers={"Accept": "application/json"}, timeout=self.timeout)
        except Exception as e:
            result.error = f"Accounts request failed: {type(e).__name__}: {e}"
            return result

        result.http_status = resp.status_code
        if resp.status_code == 404:
            result.status = ACCOUNTS_STATUS_NOT_FOUND
            result.error = "HTTP 404: No registered annual accounts found in Regnskapsregisteret."
            return result
        if not (200 <= resp.status_code < 300):
            result.error = f"HTTP {resp.status_code} from Regnskapsregisteret API."
            return result

        try:
            raw = resp.json()
        except Exception as e:
            result.status = ACCOUNTS_STATUS_MALFORMED
            result.error = f"Invalid JSON response from Regnskapsregisteret API: {e}"
            return result

        accounts_list: List[Dict[str, Any]] = []
        if isinstance(raw, list):
            accounts_list = [item for item in raw if isinstance(item, dict)]
        elif isinstance(raw, dict):
            if "virksomhet" in raw or "regnskapsperiode" in raw:
                accounts_list = [raw]
            elif isinstance(raw.get("regnskap"), list):
                accounts_list = [item for item in raw["regnskap"] if isinstance(item, dict)]
            elif isinstance(raw.get("_embedded", {}).get("regnskap"), list):
                accounts_list = [item for item in raw["_embedded"]["regnskap"] if isinstance(item, dict)]
            else:
                result.status = ACCOUNTS_STATUS_MALFORMED
                result.error = "Unexpected JSON dict format for accounts."
                return result
        else:
            result.status = ACCOUNTS_STATUS_MALFORMED
            result.error = "Accounts response is neither JSON list nor object."
            return result

        if not accounts_list:
            result.status = ACCOUNTS_STATUS_EMPTY
            result.payload = []
            return result

        # Strict identity verification: every account object must match requested org number
        for acc in accounts_list:
            v_org = acc.get("virksomhet", {}).get("organisasjonsnummer") if isinstance(acc.get("virksomhet"), dict) else None
            if not v_org or str(v_org).strip() != valid_org:
                result.status = ACCOUNTS_STATUS_FAILED
                result.error = f"Identity mismatch: account payload org number '{v_org}' does not match requested '{valid_org}'."
                return result

        result.payload = accounts_list
        result.status = ACCOUNTS_STATUS_SUCCESS
        return result


def extract_account_facts_and_evidence(
    accounts_payload: List[Dict[str, Any]],
    org_number: str,
    retrieved_at: Optional[str] = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Extract normalized period-specific, scope-aware financial key figures from Regnskapsregisteret payload.
    - Sorts multiple filings for the same period deterministically by journalnr/id (latest revision selected).
    - Formats collision-free period keys: accounts_<period_key>_<scope>_<field_name>.
    - Enforces strict whitelist and numeric precision checks.
    Returns list of (fact_key, deterministic_json_value, evidence_meta).
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    url = ACCOUNTS_URL_TEMPLATE.format(org=valid_org)

    # 1. Group records by period and scope, then sort by journalnr/id descending to pick the latest revision
    period_groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for acc in accounts_payload:
        if not isinstance(acc, dict):
            continue

        r_type = str(acc.get("regnskapstype") or "SELSKAP").upper()
        scope = "konsern" if r_type == "KONSERN" else "selskap"

        periode = acc.get("regnskapsperiode") if isinstance(acc.get("regnskapsperiode"), dict) else {}
        fra_dato = str(periode.get("fraDato") or "")
        til_dato = str(periode.get("tilDato") or "")

        if not fra_dato or not til_dato:
            continue

        group_key = (fra_dato, til_dato, scope)
        period_groups.setdefault(group_key, []).append(acc)

    # Helper for sorting revisions: journalnr (numeric string), then id (int)
    def revision_sort_key(acc: Dict[str, Any]) -> Tuple[int, int]:
        j_str = str(acc.get("journalnr") or "0")
        j_num = int(j_str) if j_str.isdigit() else 0
        a_id = acc.get("id")
        i_num = int(a_id) if isinstance(a_id, (int, str)) and str(a_id).isdigit() else 0
        return (j_num, i_num)

    out: List[Tuple[str, str, Dict[str, Any]]] = []

    for (fra_dato, til_dato, scope), records in sorted(period_groups.items()):
        # Sort descending so the latest resubmitted revision is processed first
        sorted_records = sorted(records, key=revision_sort_key, reverse=True)
        acc = sorted_records[0]

        r_type = "KONSERN" if scope == "konsern" else "SELSKAP"
        valuta = str(acc.get("valuta") or "NOK").upper()
        journalnr = acc.get("journalnr")

        year = None
        if len(til_dato) >= 4 and til_dato[:4].isdigit():
            year = til_dato[:4]
        elif len(fra_dato) >= 4 and fra_dato[:4].isdigit():
            year = fra_dato[:4]

        if not year:
            continue

        # Period tag: standard calendar year (YYYY-01-01 to YYYY-12-31) uses 'YYYY'.
        # Non-calendar or short periods use 'YYYY_YYYYMMDD_YYYYMMDD' to prevent any key collisions.
        if fra_dato == f"{year}-01-01" and til_dato == f"{year}-12-31":
            period_tag = year
        else:
            f_nodash = fra_dato.replace("-", "")
            t_nodash = til_dato.replace("-", "")
            period_tag = f"{year}_{f_nodash}_{t_nodash}"

        for path, field_name in WHITELISTED_FINANCIAL_FIELDS:
            raw_val = _get_nested(acc, path)
            num_val = sanitize_numeric_amount(raw_val)
            if num_val is None:
                continue

            fact_key = f"accounts_{period_tag}_{scope}_{field_name}"

            fact_value_dict = {
                "amount": num_val,
                "currency": valuta,
                "field_name": field_name,
                "org_number": valid_org,
                "period_from": fra_dato,
                "period_to": til_dato,
                "regnskapstype": r_type,
                "scope": scope,
                "year": int(year),
            }
            if journalnr:
                fact_value_dict["journalnr"] = str(journalnr)

            fact_value_str = json.dumps(fact_value_dict, ensure_ascii=False, sort_keys=True)

            raw_ev_dict = {
                "journalnr": journalnr,
                "org_number": valid_org,
                "regnskapsperiode": {"fraDato": fra_dato, "tilDato": til_dato},
                "regnskapstype": r_type,
                "valuta": valuta,
                "field": field_name,
                "value": num_val,
            }

            out.append((
                fact_key,
                fact_value_str,
                {
                    "source_id": ACCOUNTS_SOURCE_ID,
                    "source_name": ACCOUNTS_SOURCE_NAME,
                    "exact_source_url": url,
                    "retrieved_at": now_iso,
                    "source_validity_date": til_dato,
                    "verification_status": "source_asserted",
                    "raw_evidence": json.dumps(raw_ev_dict, ensure_ascii=False, sort_keys=True),
                }
            ))

    return out


def save_account_facts_to_storage(
    conn: sqlite3.Connection,
    org_number: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist structured financial key figures for one company.
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, 'https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}', 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (ACCOUNTS_SOURCE_ID, ACCOUNTS_SOURCE_NAME))

        existing = {row["fact_key"]: dict(row) for row in conn.execute("""
            SELECT fact_id, fact_key, fact_value FROM facts
            WHERE org_number = ? AND is_active = 1 AND fact_key LIKE 'accounts\\_%' ESCAPE '\\';
        """, (valid_org,)).fetchall()}
        new_keys = {k for k, _, _ in extracted_facts}

        if complete_set:
            for old_key, old in existing.items():
                if old_key not in new_keys:
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (old["fact_id"],))
                    conn.execute("""
                        INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                        VALUES (?, ?, ?, '', 'removed', ?, ?);
                    """, (valid_org, old_key, old["fact_value"], now_iso,
                          "Financial fact absent from latest Regnskapsregisteret response."))
                    counts["removed"] += 1

        for key, value, ev in extracted_facts:
            old = existing.get(key)
            if old and old["fact_value"] == value:
                conn.execute("UPDATE facts SET last_observed_at = ? WHERE fact_id = ?;", (now_iso, old["fact_id"]))
                fact_id = old["fact_id"]
                counts["unchanged"] += 1
            else:
                if old:
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (old["fact_id"],))
                cur = conn.execute("""
                    INSERT INTO facts (org_number, fact_key, fact_value, verification_status, first_observed_at, last_observed_at, is_active)
                    VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                """, (valid_org, key, value, now_iso, now_iso))
                fact_id = cur.lastrowid
                c_type = "updated" if old else "created"
                conn.execute("""
                    INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (valid_org, key, old["fact_value"] if old else None, value, c_type, now_iso,
                      "Financial fact updated in Regnskapsregisteret." if old else "First observation of financial fact from Regnskapsregisteret."))
                counts[c_type] += 1

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (fact_id, ACCOUNTS_SOURCE_ID, ev["exact_source_url"], ev["retrieved_at"],
                  ev.get("source_validity_date"), ev.get("raw_evidence")))

    return counts
