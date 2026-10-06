"""
Official Brønnøysund Signature Rights & Procuration (Fullmakttjenesten) open API integration for Signalpost.

Endpoints (open, no authentication required):
    GET https://data.brreg.no/fullmakt/enheter/{orgnr}/signatur
    GET https://data.brreg.no/fullmakt/enheter/{orgnr}/prokura

Responsibilities:
- Budget-enforced, identity-verified retrieval of signature and procuration authority rules.
- Strict privacy barrier: recursively strip national identity numbers, birth dates (fodselsdato, fnr, etc.),
  and D-numbers before saving to SQLite or raw evidence.
- Holder-stable, normalized facts for text rules, combinations, and authority roles.
- Conservative status semantics: distinguish success, success_empty, unsupported_org_form, not_found, failed, malformed, budget_exhausted.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple
import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.storage import init_db

logger = logging.getLogger(__name__)

FULLMAKT_SIGNATUR_SOURCE_ID = "brreg_fullmakt_signatur"
FULLMAKT_SIGNATUR_SOURCE_NAME = "Brønnøysund Register Centre - Signature Rights API"
FULLMAKT_SIGNATUR_URL_TEMPLATE = "https://data.brreg.no/fullmakt/enheter/{org}/signatur"

FULLMAKT_PROKURA_SOURCE_ID = "brreg_fullmakt_prokura"
FULLMAKT_PROKURA_SOURCE_NAME = "Brønnøysund Register Centre - Procuration Rights API"
FULLMAKT_PROKURA_URL_TEMPLATE = "https://data.brreg.no/fullmakt/enheter/{org}/prokura"

# Fetch outcome statuses (exposed in source_status)
FULLMAKT_STATUS_SUCCESS = "success"                      # identity-verified response with signature/prokura authority rules
FULLMAKT_STATUS_EMPTY = "success_empty"                  # identity-verified response with zero authority rules/combinations
FULLMAKT_STATUS_UNSUPPORTED_ORG_FORM = "unsupported_org_form" # rutineStatus.kode == "NA" (machine lookup not supported for org form)
FULLMAKT_STATUS_NOT_FOUND = "not_found"                  # HTTP 404: entity fullmakt resource not found
FULLMAKT_STATUS_FAILED = "failed"                        # network error, timeout, HTTP error, or identity mismatch
FULLMAKT_STATUS_MALFORMED = "malformed"                  # invalid JSON or unexpected schema
FULLMAKT_STATUS_BUDGET = "budget_exhausted"              # request blocked before transmission

# Keys stripped recursively from any stored/handled copy of a fullmakt payload.
SENSITIVE_PERSON_KEYS = frozenset({
    "fodselsdato", "fødselsdato", "foedselsdato",
    "fodselsnummer", "fødselsnummer", "foedselsnummer",
    "fnr", "personnummer", "personidentifikator", "pid",
    "birthdate", "birth_date", "dateofbirth", "date_of_birth",
    "d-number", "d_number", "dnummer", "d-nummer",
})


def redact_person_privacy(data: Any) -> Any:
    """
    Recursively remove birth-date, identity number, and D-number fields from a payload copy.
    Ensures zero sensitive personal identifiers are stored in SQLite or raw evidence.
    """
    if isinstance(data, dict):
        return {
            k: redact_person_privacy(v)
            for k, v in data.items()
            if str(k).lower() not in SENSITIVE_PERSON_KEYS
        }
    if isinstance(data, list):
        return [redact_person_privacy(item) for item in data]
    return data


@dataclass
class FullmaktFetchResult:
    fullmakt_type: str  # 'signatur' | 'prokura'
    status: str
    http_status: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None  # Sanitized response dict
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_set(self) -> bool:
        """True for successful, empty, or unsupported org form responses (safe for fact removal detection)."""
        return self.status in (
            FULLMAKT_STATUS_SUCCESS,
            FULLMAKT_STATUS_EMPTY,
            FULLMAKT_STATUS_UNSUPPORTED_ORG_FORM,
        )


class BrregFullmaktClient:
    """
    Budget-enforced, privacy-sanitized client for the public Brønnøysund Fullmakttjenesten API endpoints.
    """

    def __init__(
        self,
        client: Optional[BrregClient] = None,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        timeout: float = 10.0,
    ):
        self.client = client or BrregClient()
        self.session = session or self.client.session
        self.budget_tracker = budget_tracker
        self.timeout = timeout

    def _fetch_endpoint(self, org_number: str, fullmakt_type: str) -> FullmaktFetchResult:
        valid_org = BrregClient.validate_org_number(org_number)
        url_template = (
            FULLMAKT_SIGNATUR_URL_TEMPLATE
            if fullmakt_type == "signatur"
            else FULLMAKT_PROKURA_URL_TEMPLATE
        )
        url = url_template.format(org=valid_org)
        req_type = f"{fullmakt_type}_fetch"

        if self.budget_tracker and not self.budget_tracker.can_make_request():
            raise RequestBudgetExceededError(
                f"Cannot make outbound {fullmakt_type} request to {url}: budget limit exceeded."
            )

        try:
            if self.budget_tracker:
                self.budget_tracker.record_request(req_type, url)

            resp = self.session.get(url, timeout=self.timeout, headers={"Accept": "application/json"})
            http_code = resp.status_code

            if http_code == 404:
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_NOT_FOUND,
                    http_status=404,
                    source_url=url,
                )

            if http_code != 200:
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_FAILED,
                    http_status=http_code,
                    error=f"HTTP status {http_code}",
                    source_url=url,
                )

            try:
                raw_data = resp.json()
            except Exception as e:
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_MALFORMED,
                    http_status=200,
                    error=f"JSON decode failure: {e}",
                    source_url=url,
                )

            if not isinstance(raw_data, dict):
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_MALFORMED,
                    http_status=200,
                    error="Expected top-level JSON object in Fullmakt response.",
                    source_url=url,
                )

            sanitized_data = redact_person_privacy(raw_data)

            # Identity Verification
            returned_org = (
                sanitized_data.get("enhet", {}).get("organisasjonsnummer")
                if isinstance(sanitized_data.get("enhet"), dict)
                else None
            )
            if returned_org and str(returned_org).strip() != valid_org:
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_FAILED,
                    http_status=200,
                    error=f"Identity mismatch: expected org {valid_org}, got {returned_org}",
                    source_url=url,
                )

            # Check routine status
            rutine_status = (
                sanitized_data.get("status", {}).get("rutineStatus", {}).get("kode")
                if isinstance(sanitized_data.get("status"), dict)
                else None
            )
            if rutine_status == "NA":
                return FullmaktFetchResult(
                    fullmakt_type=fullmakt_type,
                    status=FULLMAKT_STATUS_UNSUPPORTED_ORG_FORM,
                    http_status=200,
                    payload=sanitized_data,
                    source_url=url,
                )

            # Determine whether rules/combinations are present
            grunnlag = sanitized_data.get("signeringsGrunnlag") or {}
            kombinasjon = sanitized_data.get("signeringsKombinasjon") or {}
            has_text = bool(grunnlag.get("signaturProkuraFritekst"))
            has_roles = bool(grunnlag.get("muligeSigneringsRoller"))
            has_komb = bool(kombinasjon.get("kombinasjon"))

            if not (has_text or has_roles or has_komb):
                status = FULLMAKT_STATUS_EMPTY
            else:
                status = FULLMAKT_STATUS_SUCCESS

            return FullmaktFetchResult(
                fullmakt_type=fullmakt_type,
                status=status,
                http_status=200,
                payload=sanitized_data,
                source_url=url,
            )

        except RequestBudgetExceededError:
            raise
        except requests.RequestException as e:
            return FullmaktFetchResult(
                fullmakt_type=fullmakt_type,
                status=FULLMAKT_STATUS_FAILED,
                error=f"Network error: {e}",
                source_url=url,
            )
        except Exception as e:
            return FullmaktFetchResult(
                fullmakt_type=fullmakt_type,
                status=FULLMAKT_STATUS_FAILED,
                error=f"Unexpected error: {e}",
                source_url=url,
            )

    def fetch_signatur(self, org_number: str) -> FullmaktFetchResult:
        return self._fetch_endpoint(org_number, "signatur")

    def fetch_prokura(self, org_number: str) -> FullmaktFetchResult:
        return self._fetch_endpoint(org_number, "prokura")


def extract_fullmakt_facts_and_evidence(
    payload: Dict[str, Any],
    org_number: str,
    fullmakt_type: str,
    source_url: str,
    retrieved_at: Optional[str] = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Extract normalized facts and evidence linkage from a sanitized signature/prokura payload.
    Returns list of (fact_key, fact_value, evidence_dict).
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()

    # Double check privacy sanitization
    sanitized_payload = redact_person_privacy(payload)
    raw_ev_str = json.dumps(sanitized_payload, ensure_ascii=False, sort_keys=True)

    evidence_dict = {
        "exact_source_url": source_url,
        "retrieved_at": now_iso,
        "source_validity_date": None,
        "raw_evidence": raw_ev_str,
    }

    facts: List[Tuple[str, str, Dict[str, Any]]] = []
    prefix = f"fullmakt_{fullmakt_type}_"

    grunnlag = sanitized_payload.get("signeringsGrunnlag") or {}
    kombinasjon_section = sanitized_payload.get("signeringsKombinasjon") or {}

    # 1. Text Rule (signaturProkuraFritekst)
    friteks = grunnlag.get("signaturProkuraFritekst")
    if friteks and isinstance(friteks, str) and friteks.strip():
        facts.append((f"{prefix}text_rule", friteks.strip(), evidence_dict))

    # 2. Combinations (signeringsKombinasjon.kombinasjon)
    komb_list = kombinasjon_section.get("kombinasjon") or []
    if isinstance(komb_list, list):
        for idx, komb in enumerate(komb_list):
            if isinstance(komb, dict):
                k_id = komb.get("kombinasjonsId") or (idx + 1)
                explanation = komb.get("tekstforklaring") or ""
                code = komb.get("kode") or ""
                fact_val = f"{code}: {explanation}".strip(" :") if code else explanation
                if fact_val:
                    facts.append((f"{prefix}combination_{k_id}", fact_val, evidence_dict))

    # 3. Eligible signing roles (muligeSigneringsRoller)
    roles_list = grunnlag.get("muligeSigneringsRoller") or []
    if isinstance(roles_list, list):
        for idx, r_item in enumerate(roles_list):
            if isinstance(r_item, dict):
                p_grunnlag = r_item.get("personRolleGrunnlag") or {}
                r_info = p_grunnlag.get("rolle") or {}
                r_code = r_info.get("kode") or f"ROLE_{idx+1}"
                name = p_grunnlag.get("navn") or ""
                if name:
                    holder_key = " ".join(name.lower().split())
                    fact_val = json.dumps({"role": r_code, "name": name}, ensure_ascii=False, sort_keys=True)
                    facts.append((f"{prefix}role_{r_code.lower()}_{hashlib_token(holder_key)}", fact_val, evidence_dict))

    return facts


def hashlib_token(s: str) -> str:
    """Generate a short stable hash token for key construction."""
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]


def save_fullmakt_facts_to_storage(
    conn: sqlite3.Connection,
    org_number: str,
    fullmakt_type: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist fullmakt facts (signatur or prokura) for one company into SQLite.
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    source_id = FULLMAKT_SIGNATUR_SOURCE_ID if fullmakt_type == "signatur" else FULLMAKT_PROKURA_SOURCE_ID
    source_name = FULLMAKT_SIGNATUR_SOURCE_NAME if fullmakt_type == "signatur" else FULLMAKT_PROKURA_SOURCE_NAME
    url_template = FULLMAKT_SIGNATUR_URL_TEMPLATE if fullmakt_type == "signatur" else FULLMAKT_PROKURA_URL_TEMPLATE

    prefix = f"fullmakt_{fullmakt_type}_%"

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, ?, 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (source_id, source_name, url_template))

        existing = {
            row["fact_key"]: dict(row)
            for row in conn.execute("""
                SELECT fact_id, fact_key, fact_value FROM facts
                WHERE org_number = ? AND is_active = 1 AND fact_key LIKE ?;
            """, (valid_org, prefix)).fetchall()
        }
        new_keys = {k for k, _, _ in extracted_facts}

        if complete_set:
            for old_key, old in existing.items():
                if old_key not in new_keys:
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (old["fact_id"],))
                    conn.execute("""
                        INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                        VALUES (?, ?, ?, '', 'removed', ?, ?);
                    """, (
                        valid_org,
                        old_key,
                        old["fact_value"],
                        now_iso,
                        f"Fact absent from complete {fullmakt_type} response. No reason inferred.",
                    ))
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
                """, (
                    valid_org,
                    key,
                    old["fact_value"] if old else None,
                    value,
                    c_type,
                    now_iso,
                    f"{fullmakt_type.capitalize()} rule updated." if old else f"First observation of {fullmakt_type} rule.",
                ))
                counts[c_type] += 1

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (
                fact_id,
                source_id,
                ev["exact_source_url"],
                ev["retrieved_at"],
                ev.get("source_validity_date"),
                ev.get("raw_evidence"),
            ))

    return counts
