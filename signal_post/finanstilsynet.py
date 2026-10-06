"""
Finanstilsynet Virksomhetsregisteret API integration for Signalpost.

Endpoint (open, no authentication required):
    GET https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org_number}

Responsibilities:
- Budget-enforced, identity-verified exact query of Finanstilsynet regulatory registry.
- Strict company-only scope: matches only legal entities where organisationNumber equals the requested 9-digit org number and legalEntityType != 'Person'.
- Privacy minimization: excludes personal residential details, birth dates, or national identity numbers.
- Extraction of regulatory facts: Finanstilsynet ID, LEI code, and active licences.
- Conservative status semantics: distinguish 'success' from 'not_registered', 'failed', 'malformed', 'budget_exhausted'.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.storage import init_db

logger = logging.getLogger(__name__)

FINANSTILSYNET_SOURCE_ID = "finanstilsynet_registry"
FINANSTILSYNET_SOURCE_NAME = "Finanstilsynet Virksomhetsregisteret"
FINANSTILSYNET_URL_TEMPLATE = "https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org}"

# Outcome status categories
FINANSTILSYNET_STATUS_SUCCESS = "success"             # Matching company entity found with licences/registry entries
FINANSTILSYNET_STATUS_NOT_REGISTERED = "not_registered" # Query completed cleanly; company not present in current registry
FINANSTILSYNET_STATUS_NOT_FOUND = "not_found"         # HTTP 404
FINANSTILSYNET_STATUS_FAILED = "failed"               # Network error, timeout, HTTP error, or identity mismatch
FINANSTILSYNET_STATUS_MALFORMED = "malformed"         # Unexpected schema or JSON decode failure
FINANSTILSYNET_STATUS_BUDGET = "budget_exhausted"     # Outbound request blocked before transmission

SENSITIVE_PERSON_KEYS = frozenset({
    "fodselsdato", "fødselsdato", "foedselsdato",
    "fodselsnummer", "fødselsnummer", "foedselsnummer",
    "fnr", "personnummer", "personidentifikator", "pid",
    "birthdate", "birth_date", "residentialaddress", "homeaddress",
})


def sanitize_finanstilsynet_privacy(data: Any) -> Any:
    """
    Recursively remove personal residential address, birth date, and identity fields.
    """
    if isinstance(data, dict):
        return {
            k: sanitize_finanstilsynet_privacy(v)
            for k, v in data.items()
            if str(k).lower() not in SENSITIVE_PERSON_KEYS
        }
    if isinstance(data, list):
        return [sanitize_finanstilsynet_privacy(item) for item in data]
    return data


@dataclass
class FinanstilsynetFetchResult:
    status: str
    http_status: Optional[int] = None
    entity_data: Optional[Dict[str, Any]] = None  # Sanitized matching company dict
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_set(self) -> bool:
        """True only for successful or clean not_registered responses (safe basis for removal detection)."""
        return self.status in (FINANSTILSYNET_STATUS_SUCCESS, FINANSTILSYNET_STATUS_NOT_REGISTERED)


class FinanstilsynetClient:
    """Budget-enforced client for the open Finanstilsynet Virksomhetsregisteret API v2."""

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        timeout: float = 10.0,
    ):
        self.session = session or requests.Session()
        self.budget_tracker = budget_tracker
        self.timeout = timeout

    def fetch_registry(self, org_number: str) -> FinanstilsynetFetchResult:
        """
        Query Finanstilsynet v2 filter endpoint for a 9-digit Norwegian organization number.
        Returns a classified FinanstilsynetFetchResult. Never raises for network/HTTP errors.
        Raises RequestBudgetExceededError if budget limit is reached before transmission.
        """
        valid_org = BrregClient.validate_org_number(org_number)
        url = FINANSTILSYNET_URL_TEMPLATE.format(org=valid_org)

        if self.budget_tracker:
            self.budget_tracker.record_request("finanstilsynet_fetch", url)

        result = FinanstilsynetFetchResult(status=FINANSTILSYNET_STATUS_FAILED, source_url=url)
        try:
            resp = self.session.get(url, headers={"Accept": "application/json"}, timeout=self.timeout)
        except Exception as e:
            result.error = f"Finanstilsynet request failed: {type(e).__name__}: {e}"
            return result

        result.http_status = resp.status_code
        if resp.status_code == 404:
            result.status = FINANSTILSYNET_STATUS_NOT_FOUND
            result.error = "HTTP 404 from Finanstilsynet API."
            return result
        if not (200 <= resp.status_code < 300):
            result.error = f"HTTP {resp.status_code} from Finanstilsynet API."
            return result

        try:
            raw = resp.json()
        except Exception as e:
            result.status = FINANSTILSYNET_STATUS_MALFORMED
            result.error = f"Invalid JSON response from Finanstilsynet API: {e}"
            return result

        if not isinstance(raw, dict):
            result.status = FINANSTILSYNET_STATUS_MALFORMED
            result.error = "Finanstilsynet response is not a JSON object."
            return result

        entities = raw.get("legalEntities")
        if not isinstance(entities, list):
            result.status = FINANSTILSYNET_STATUS_MALFORMED
            result.error = "'legalEntities' key is missing or not a list."
            return result

        # Exact company-only matching
        matching_companies = [
            e for e in entities
            if isinstance(e, dict)
            and str(e.get("organisationNumber", "")).strip() == valid_org
            and str(e.get("legalEntityType", "")).lower() != "person"
        ]

        if not matching_companies:
            result.status = FINANSTILSYNET_STATUS_NOT_REGISTERED
            return result

        company = sanitize_finanstilsynet_privacy(matching_companies[0])
        result.entity_data = company
        result.status = FINANSTILSYNET_STATUS_SUCCESS
        return result


def extract_finanstilsynet_facts_and_evidence(
    entity_data: Dict[str, Any],
    org_number: str,
    retrieved_at: Optional[str] = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Extract normalized regulatory & licence facts from a Finanstilsynet legal entity record.
    Returns list of (fact_key, fact_value_json_str, evidence_dict).
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    url = FINANSTILSYNET_URL_TEMPLATE.format(org=valid_org)

    out: List[Tuple[str, str, Dict[str, Any]]] = []

    # 1. Finanstilsynet ID
    ft_id = entity_data.get("finanstilsynetId")
    if ft_id:
        out.append((
            "finanstilsynet_id",
            str(ft_id),
            {
                "source_id": FINANSTILSYNET_SOURCE_ID,
                "source_name": FINANSTILSYNET_SOURCE_NAME,
                "exact_source_url": url,
                "retrieved_at": now_iso,
                "source_validity_date": None,
                "verification_status": "source_asserted",
                "raw_evidence": json.dumps({"finanstilsynetId": ft_id}, ensure_ascii=False, sort_keys=True),
            }
        ))

    # 2. LEI Code (if present)
    lei_code = entity_data.get("leiCode")
    if lei_code:
        out.append((
            "lei_code",
            str(lei_code),
            {
                "source_id": FINANSTILSYNET_SOURCE_ID,
                "source_name": FINANSTILSYNET_SOURCE_NAME,
                "exact_source_url": url,
                "retrieved_at": now_iso,
                "source_validity_date": None,
                "verification_status": "source_asserted",
                "raw_evidence": json.dumps({"leiCode": lei_code}, ensure_ascii=False, sort_keys=True),
            }
        ))

    # 3. Licences & Authorisations
    licences = entity_data.get("licences") or []
    for idx, lic in enumerate(licences):
        if not isinstance(lic, dict):
            continue
        lic_type = lic.get("licenceType") or {}
        code = str(lic_type.get("code") or "LICENCE").upper()
        
        # Holder/entity ID or index for stable keying
        entity_item = lic.get("licensedEntity") or {}
        ent_id = entity_item.get("legalEntityId") or idx
        fact_key = f"licence_{code.lower()}_{ent_id}"

        name_dict = lic_type.get("name") if isinstance(lic_type.get("name"), dict) else {}
        desc_dict = lic_type.get("description") if isinstance(lic_type.get("description"), dict) else {}
        supervisory = lic.get("supervisoryAuthority") if isinstance(lic.get("supervisoryAuthority"), dict) else {}

        val_dict: Dict[str, Any] = {
            "licence_code": code,
            "licence_name": name_dict.get("norwegian") or code,
        }
        if name_dict.get("english"):
            val_dict["licence_name_en"] = name_dict.get("english")
        if desc_dict.get("norwegian"):
            val_dict["description"] = desc_dict.get("norwegian")
        if lic.get("registeredDate"):
            val_dict["registered_date"] = lic.get("registeredDate")
        if lic.get("serviceProviderType"):
            val_dict["service_provider_type"] = lic.get("serviceProviderType")
        if supervisory.get("legalEntityName"):
            val_dict["supervisory_authority"] = supervisory.get("legalEntityName")

        val_str = json.dumps(val_dict, ensure_ascii=False, sort_keys=True)
        reg_date = lic.get("registeredDate")

        out.append((
            fact_key,
            val_str,
            {
                "source_id": FINANSTILSYNET_SOURCE_ID,
                "source_name": FINANSTILSYNET_SOURCE_NAME,
                "exact_source_url": url,
                "retrieved_at": now_iso,
                "source_validity_date": reg_date[:10] if isinstance(reg_date, str) and len(reg_date) >= 10 else None,
                "verification_status": "source_asserted",
                "raw_evidence": json.dumps({"licence": val_dict}, ensure_ascii=False, sort_keys=True),
            }
        ))

    return out


def save_finanstilsynet_facts_to_storage(
    conn: sqlite3.Connection,
    org_number: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist Finanstilsynet regulatory facts for one company.
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, 'https://api.finanstilsynet.no/registry/v2/legal-entities/filter?query={org}', 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (FINANSTILSYNET_SOURCE_ID, FINANSTILSYNET_SOURCE_NAME))

        existing = {row["fact_key"]: dict(row) for row in conn.execute("""
            SELECT fact_id, fact_key, fact_value FROM facts
            WHERE org_number = ? AND is_active = 1 AND (fact_key LIKE 'licence\\_%' ESCAPE '\\' OR fact_key IN ('finanstilsynet_id', 'lei_code'));
        """, (valid_org,)).fetchall()}
        new_keys = {k for k, _, _ in extracted_facts}

        if complete_set:
            for old_key, old in existing.items():
                if old_key not in new_keys:
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (old["fact_id"],))
                    conn.execute("""
                        INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                        VALUES (?, ?, ?, '', 'no_longer_present_in_current_registry', ?, ?);
                    """, (valid_org, old_key, old["fact_value"], now_iso,
                          "Regulatory entry or licence no longer present in current Finanstilsynet registry response."))
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
                      "Regulatory fact updated in Finanstilsynet registry." if old else "First observation of regulatory fact from Finanstilsynet."))
                counts[c_type] += 1

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (fact_id, FINANSTILSYNET_SOURCE_ID, ev["exact_source_url"], ev["retrieved_at"],
                  ev.get("source_validity_date"), ev.get("raw_evidence")))

    return counts
