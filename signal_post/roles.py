"""
Official Brønnøysund Roles API integration for Signalpost.

Endpoint (open, no authentication):
    GET https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller
Official documentation describes it as "Hent alle roller for en spesifikk enhet"
(retrieve all roles for a specific entity). Endpoints that include national identity
numbers live under /autorisert-api/ (Maskinporten-secured) and are deliberately NOT used.

Responsibilities:
- Budget-enforced, identity-verified retrieval of the role set for one organization.
- Privacy minimization: birth dates / identity numbers are stripped before anything is
  normalized, persisted, or output. Normalized facts are built from a field whitelist.
- Deterministic, holder-stable role fact keys (no positional indices) so that a board
  member leaving does not create false "updated" changes for other members.
- Conservative status semantics: 404 / failure / malformed are never treated as
  "this company has no roles".
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.storage import init_db

logger = logging.getLogger(__name__)

ROLES_SOURCE_ID = "brreg_roles"
ROLES_SOURCE_NAME = "Brønnøysund Register Centre - Roles API"
ROLES_URL_TEMPLATE = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"

# Fetch outcome statuses (exposed in evaluator source_status.roles)
ROLES_STATUS_SUCCESS = "success"                    # identity-verified response with >=1 role group
ROLES_STATUS_EMPTY = "success_empty"                # identity-verified response containing zero role groups
ROLES_STATUS_NOT_FOUND = "not_found"                # HTTP 404: resource not found (NOT proof of zero roles)
ROLES_STATUS_FAILED = "failed"                      # network error, timeout, HTTP error, identity mismatch
ROLES_STATUS_MALFORMED = "malformed"                # invalid JSON or unexpected schema
ROLES_STATUS_BUDGET = "budget_exhausted"            # request blocked before transmission

# Keys stripped recursively from any stored/handled copy of a roles payload.
# Includes Norwegian spelling variants and common English equivalents.
SENSITIVE_PERSON_KEYS = frozenset({
    "fodselsdato", "fødselsdato", "foedselsdato",
    "fodselsnummer", "fødselsnummer", "foedselsnummer",
    "fnr", "personnummer", "personidentifikator", "pid",
    "birthdate", "birth_date", "dateofbirth", "date_of_birth",
})


def redact_person_privacy(data: Any) -> Any:
    """
    Recursively remove birth-date and national-identity fields from a payload copy.
    The Brønnøysund source itself is never modified; only Signalpost's copy is sanitized.
    """
    if isinstance(data, dict):
        return {k: redact_person_privacy(v) for k, v in data.items()
                if str(k).lower() not in SENSITIVE_PERSON_KEYS}
    if isinstance(data, list):
        return [redact_person_privacy(item) for item in data]
    return data


@dataclass
class RoleHolder:
    """A role holder: a person (name only) or an organization (name + its own org number)."""
    holder_type: str  # 'person' | 'organization'
    name: str
    org_number: Optional[str] = None
    deceased: Optional[bool] = None  # registry-provided flag (erDoed); never inferred

    def identity_token(self) -> str:
        if self.holder_type == "organization" and self.org_number:
            return f"org:{self.org_number}"
        return f"person:{' '.join(self.name.lower().split())}"

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"holder_type": self.holder_type, "name": self.name}
        if self.org_number:
            d["org_number"] = self.org_number
        if self.deceased is True:
            d["deceased"] = True
        return d


@dataclass
class NormalizedRoleItem:
    """A single active role assertion for the researched company."""
    role_group_code: str
    role_group_name: str
    role_code: str
    role_name: str
    holder: RoleHolder
    sequence: int  # registry ordering; used for deterministic sorting only, not part of fact value
    elected_by: Optional[str] = None
    group_last_changed: Optional[str] = None  # 'sistEndret' supplied explicitly by the source

    def to_fact_value(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "role_group_code": self.role_group_code,
            "role_group_name": self.role_group_name,
            "role_code": self.role_code,
            "role_name": self.role_name,
            "holder": self.holder.to_dict(),
        }
        if self.elected_by:
            d["elected_by"] = self.elected_by
        return d

    def base_fact_key(self) -> str:
        """Holder-stable key: role_<group>_<role>_<10-hex digest of holder identity>."""
        digest = hashlib.sha256(self.holder.identity_token().encode("utf-8")).hexdigest()[:10]
        return f"role_{self.role_group_code.lower()}_{self.role_code.lower()}_{digest}"


@dataclass
class RolesFetchResult:
    status: str
    http_status: Optional[int] = None
    payload: Optional[Dict[str, Any]] = None  # sanitized copy only
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_role_set(self) -> bool:
        """True only for identity-verified successful responses (safe basis for removal detection)."""
        return self.status in (ROLES_STATUS_SUCCESS, ROLES_STATUS_EMPTY)


def _href(links: Any, name: str) -> Optional[str]:
    if isinstance(links, dict) and isinstance(links.get(name), dict):
        h = links[name].get("href")
        return h if isinstance(h, str) else None
    return None


def classify_roles_payload(raw: Any, org_number: str) -> Tuple[str, Optional[str]]:
    """
    Validate identity and schema of a decoded 200 response.
    :return: (status, error_message)
    """
    if not isinstance(raw, dict):
        return ROLES_STATUS_MALFORMED, "Roles response is not a JSON object."
    links = raw.get("_links")
    self_href = _href(links, "self")
    enhet_href = _href(links, "enhet")
    if not self_href and not enhet_href:
        return ROLES_STATUS_MALFORMED, "Roles response lacks _links identity context; cannot verify company."
    if self_href and not self_href.rstrip("/").endswith(f"/enheter/{org_number}/roller"):
        return ROLES_STATUS_FAILED, f"Identity mismatch: response self link '{self_href}' does not match {org_number}."
    if enhet_href and not enhet_href.rstrip("/").endswith(f"/enheter/{org_number}"):
        return ROLES_STATUS_FAILED, f"Identity mismatch: response enhet link '{enhet_href}' does not match {org_number}."
    groups = raw.get("rollegrupper")
    if groups is None:
        return ROLES_STATUS_EMPTY, None
    if not isinstance(groups, list):
        return ROLES_STATUS_MALFORMED, "'rollegrupper' is not a list."
    if any(not isinstance(g, dict) or not isinstance(g.get("roller", []), list) for g in groups):
        return ROLES_STATUS_MALFORMED, "Unexpected role group structure."
    return (ROLES_STATUS_SUCCESS if groups else ROLES_STATUS_EMPTY), None


class BrregRolesClient:
    """Budget-enforced client for the official open Roles API."""

    def __init__(self, client: Optional[BrregClient] = None, budget_tracker: Optional[RequestBudgetTracker] = None):
        self.client = client or BrregClient(budget_tracker=budget_tracker)
        self.budget_tracker = budget_tracker or getattr(self.client, "budget_tracker", None)

    def fetch_roles(self, org_number: str) -> RolesFetchResult:
        """
        Retrieve and classify the role set. Never raises for network/schema problems;
        raises RequestBudgetExceededError only when the request is blocked before transmission.
        """
        valid_org = BrregClient.validate_org_number(org_number)
        url = ROLES_URL_TEMPLATE.format(org=valid_org)

        if self.budget_tracker:
            # record_request raises before logging (and before transmission) when exhausted
            self.budget_tracker.record_request("roles_fetch", url)

        result = RolesFetchResult(status=ROLES_STATUS_FAILED, source_url=url)
        try:
            resp = self.client.session.get(url, headers={"Accept": "application/json"}, timeout=self.client.timeout)
        except Exception as e:  # timeout, connection error
            result.error = f"Roles request failed: {type(e).__name__}: {e}"
            return result

        result.http_status = resp.status_code
        if resp.status_code == 404:
            result.status = ROLES_STATUS_NOT_FOUND
            result.error = "HTTP 404 (resource not found); role information unavailable, not proof of zero roles."
            return result
        if not (200 <= resp.status_code < 300):
            result.error = f"HTTP {resp.status_code} from Roles API."
            return result
        try:
            raw = resp.json()
        except Exception as e:
            result.status = ROLES_STATUS_MALFORMED
            result.error = f"Invalid JSON from Roles API: {e}"
            return result

        sanitized = redact_person_privacy(raw)
        status, err = classify_roles_payload(sanitized, valid_org)
        result.status, result.error = status, err
        if status in (ROLES_STATUS_SUCCESS, ROLES_STATUS_EMPTY):
            result.payload = sanitized
        return result


def extract_normalized_roles(roles_json: Any) -> List[NormalizedRoleItem]:
    """Build normalized active roles from a (sanitized) payload using a strict field whitelist."""
    if not isinstance(roles_json, dict):
        return []
    items: List[NormalizedRoleItem] = []
    for group in roles_json.get("rollegrupper") or []:
        if not isinstance(group, dict):
            continue
        g_type = group.get("type") or {}
        g_code = str(g_type.get("kode") or "UNKNOWN").upper()
        g_name = str(g_type.get("beskrivelse") or g_code)
        last_changed = group.get("sistEndret") if isinstance(group.get("sistEndret"), str) else None

        for idx, r in enumerate(group.get("roller") or []):
            if not isinstance(r, dict) or bool(r.get("avregistrert", False)):
                continue  # deregistered roles are historical, not current assertions
            r_type = r.get("type") or {}
            r_code = str(r_type.get("kode") or g_code).upper()
            r_name = str(r_type.get("beskrivelse") or r_code)
            try:
                seq = int(r.get("rekkefolge", idx))
            except (TypeError, ValueError):
                seq = idx
            valgt = r.get("valgtAv")
            elected_by = valgt.get("beskrivelse") if isinstance(valgt, dict) else None

            holder: Optional[RoleHolder] = None
            person, enhet = r.get("person"), r.get("enhet")
            if isinstance(person, dict):
                navn = person.get("navn") or {}
                parts = [navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn")] if isinstance(navn, dict) else []
                full = " ".join(p for p in parts if isinstance(p, str) and p).strip()
                if full:
                    dead = person.get("erDoed")
                    holder = RoleHolder("person", full, deceased=dead if isinstance(dead, bool) else None)
            elif isinstance(enhet, dict):
                navn_val = enhet.get("navn")
                name = ", ".join(navn_val) if isinstance(navn_val, list) else (str(navn_val) if navn_val else "")
                org = enhet.get("organisasjonsnummer")
                if name or org:
                    holder = RoleHolder("organization", name or "Unknown", org_number=str(org) if org else None)
            if holder is None:
                continue  # e.g. bostyrer-only entries; skipped rather than guessed

            items.append(NormalizedRoleItem(g_code, g_name, r_code, r_name, holder, seq, elected_by, last_changed))

    items.sort(key=lambda x: (x.role_group_code, x.role_code, x.sequence, x.holder.identity_token()))
    return items


def extract_role_facts_and_evidence(
    roles_json: Dict[str, Any],
    org_number: str,
    retrieved_at: Optional[str] = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """:return: list of (fact_key, deterministic_json_value, evidence_meta) for the researched company."""
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    url = ROLES_URL_TEMPLATE.format(org=valid_org)

    out: List[Tuple[str, str, Dict[str, Any]]] = []
    seen: Dict[str, int] = {}
    for item in extract_normalized_roles(roles_json):
        base = item.base_fact_key()
        n = seen.get(base, 0) + 1
        seen[base] = n
        key = base if n == 1 else f"{base}_{n}"  # same-name holders in the same role
        value = json.dumps(item.to_fact_value(), ensure_ascii=False, sort_keys=True)
        out.append((key, value, {
            "source_id": ROLES_SOURCE_ID,
            "source_name": ROLES_SOURCE_NAME,
            "exact_source_url": url,
            "retrieved_at": now_iso,
            # 'sistEndret' is an explicit source-supplied last-changed date for the role group
            "source_validity_date": item.group_last_changed,
            "verification_status": "source_asserted",
            # Sanitized, whitelisted role evidence (no birth dates / identity numbers)
            "raw_evidence": json.dumps({"requested_org_number": valid_org,
                                        "role_group_last_changed": item.group_last_changed,
                                        "role": item.to_fact_value()}, ensure_ascii=False, sort_keys=True),
        }))
    return out


def save_role_facts_to_storage(
    conn: sqlite3.Connection,
    org_number: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_role_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist role facts for one company.

    - New role            -> new source_asserted fact + 'created' history.
    - Same role re-seen   -> last_observed_at updated + new evidence row; no history entry.
    - Same holder/role, changed attributes -> old fact deactivated, new fact + 'updated' history.
    - Role absent from a complete, identity-verified role set -> deactivated + 'removed' history
      (only when complete_role_set=True; no reason is inferred).
    """
    valid_org = BrregClient.validate_org_number(org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, 'https://data.brreg.no/enhetsregisteret/api/enheter/{orgnr}/roller', 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (ROLES_SOURCE_ID, ROLES_SOURCE_NAME))

        existing = {row["fact_key"]: dict(row) for row in conn.execute("""
            SELECT fact_id, fact_key, fact_value FROM facts
            WHERE org_number = ? AND is_active = 1 AND fact_key LIKE 'role\\_%' ESCAPE '\\';
        """, (valid_org,)).fetchall()}
        new_keys = {k for k, _, _ in extracted_facts}

        if complete_role_set:
            for old_key, old in existing.items():
                if old_key not in new_keys:
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (old["fact_id"],))
                    conn.execute("""
                        INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                        VALUES (?, ?, ?, '', 'removed', ?, ?);
                    """, (valid_org, old_key, old["fact_value"], now_iso,
                          "Role absent from the complete current role set returned by the official Roles API. No reason inferred."))
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
                      "Role attributes changed in official Roles API." if old else "First observation of role from official Roles API."))
                counts[c_type] += 1

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (fact_id, ROLES_SOURCE_ID, ev["exact_source_url"], ev["retrieved_at"],
                  ev.get("source_validity_date"), ev.get("raw_evidence")))

    return counts
