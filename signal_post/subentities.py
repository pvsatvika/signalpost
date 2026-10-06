"""
Official Brønnøysund Underenheter (Operating Units) Open Data API integration for Signalpost.

Endpoint (open, no authentication required):
    GET https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_number}

Responsibilities:
- Budget-enforced, identity-verified retrieval of operating units (underenheter) for a parent legal entity.
- Strict parent/child identity separation: subentity attributes (address, employees, activity) remain strictly
  child-scoped and are NEVER merged into the parent legal entity's profile.
- Controlled pagination with bounded safety limits (max pages, max subentities) to prevent infinite loops.
- Support for missing/suppressed employee counts (`harRegistrertAntallAnsatte` without exact number).
- Conservative status semantics: distinguish success, success_empty, truncated, not_found, failed, malformed, budget_exhausted.
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

SUBENTITIES_SOURCE_ID = "brreg_subentities"
SUBENTITIES_SOURCE_NAME = "Brønnøysund Register Centre - Underenheter API"
SUBENTITIES_URL_TEMPLATE = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}"

# Outcome status categories
SUBENTITIES_STATUS_SUCCESS = "success"            # Identity-verified response with >=1 subentities
SUBENTITIES_STATUS_EMPTY = "success_empty"        # Identity-verified response with zero subentities
SUBENTITIES_STATUS_TRUNCATED = "truncated"        # Fetch completed up to pagination/size limit (results truncated)
SUBENTITIES_STATUS_NOT_FOUND = "not_found"        # HTTP 404
SUBENTITIES_STATUS_FAILED = "failed"              # Network error, timeout, HTTP error, or identity mismatch
SUBENTITIES_STATUS_MALFORMED = "malformed"        # Invalid JSON or unexpected schema
SUBENTITIES_STATUS_BUDGET = "budget_exhausted"    # Request blocked before transmission


@dataclass
class SubentitiesFetchResult:
    status: str
    http_status: Optional[int] = None
    subentities: List[Dict[str, Any]] = field(default_factory=list)
    total_elements: int = 0
    pages_fetched: int = 0
    is_truncated: bool = False
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_set(self) -> bool:
        """True for successful or empty responses where all items were fetched without truncation."""
        return self.status in (SUBENTITIES_STATUS_SUCCESS, SUBENTITIES_STATUS_EMPTY) and not self.is_truncated


class BrregSubentitiesClient:
    """
    Budget-enforced, paginated client for official Brønnøysund Underenheter API.
    """

    def __init__(
        self,
        client: Optional[BrregClient] = None,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        timeout: float = 10.0,
        page_size: int = 100,
        max_pages: int = 5,
        max_subentities: int = 200,
    ):
        self.client = client or BrregClient()
        self.session = session or self.client.session
        self.budget_tracker = budget_tracker
        self.timeout = timeout
        self.page_size = page_size
        self.max_pages = max_pages
        self.max_subentities = max_subentities

    def fetch_subentities(self, parent_org_number: str) -> SubentitiesFetchResult:
        """
        Query Brønnøysund underenheter endpoint for a 9-digit parent organization number.
        Executes bounded pagination. Never raises for network/HTTP errors.
        """
        valid_parent_org = BrregClient.validate_org_number(parent_org_number)
        base_url = f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={valid_parent_org}"

        collected_subentities: List[Dict[str, Any]] = []
        total_elements = 0
        pages_fetched = 0
        is_truncated = False

        page_idx = 0
        while page_idx < self.max_pages:
            url = f"{base_url}&page={page_idx}&size={self.page_size}&sort=organisasjonsnummer,ASC"

            if self.budget_tracker and not self.budget_tracker.can_make_request():
                if page_idx == 0:
                    raise RequestBudgetExceededError(
                        f"Cannot make outbound underenheter request to {url}: budget limit exceeded."
                    )
                else:
                    is_truncated = True
                    break

            try:
                if self.budget_tracker:
                    self.budget_tracker.record_request("subentities_fetch", url)

                pages_fetched += 1
                resp = self.session.get(url, timeout=self.timeout, headers={"Accept": "application/json"})
                http_code = resp.status_code

                if http_code == 404:
                    if page_idx == 0:
                        return SubentitiesFetchResult(
                            status=SUBENTITIES_STATUS_NOT_FOUND,
                            http_status=404,
                            source_url=url,
                        )
                    break

                if http_code != 200:
                    if page_idx == 0:
                        return SubentitiesFetchResult(
                            status=SUBENTITIES_STATUS_FAILED,
                            http_status=http_code,
                            error=f"HTTP status {http_code}",
                            source_url=url,
                        )
                    break

                try:
                    data = resp.json()
                except Exception as e:
                    if page_idx == 0:
                        return SubentitiesFetchResult(
                            status=SUBENTITIES_STATUS_MALFORMED,
                            http_status=200,
                            error=f"JSON decode failure: {e}",
                            source_url=url,
                        )
                    break

                if not isinstance(data, dict):
                    if page_idx == 0:
                        return SubentitiesFetchResult(
                            status=SUBENTITIES_STATUS_MALFORMED,
                            http_status=200,
                            error="Expected top-level JSON object in underenheter response.",
                            source_url=url,
                        )
                    break

                page_info = data.get("page") or {}
                total_elements = page_info.get("totalElements", total_elements)
                total_pages = page_info.get("totalPages", 1)

                embedded = data.get("_embedded") or {}
                items = embedded.get("underenheter") or []

                if not isinstance(items, list):
                    if page_idx == 0:
                        return SubentitiesFetchResult(
                            status=SUBENTITIES_STATUS_MALFORMED,
                            http_status=200,
                            error="Expected '_embedded.underenheter' to be a list.",
                            source_url=url,
                        )
                    break

                for item in items:
                    if isinstance(item, dict):
                        parent_link_org = item.get("overordnetEnhet")
                        # Identity boundary check: verify returned item is actually a child of the requested parent
                        if parent_link_org and str(parent_link_org).strip() != valid_parent_org:
                            logger.warning(
                                f"Subentity {item.get('organisasjonsnummer')} overordnetEnhet mismatch: "
                                f"expected {valid_parent_org}, got {parent_link_org}. Skipping."
                            )
                            continue
                        collected_subentities.append(item)

                if len(collected_subentities) >= self.max_subentities:
                    collected_subentities = collected_subentities[: self.max_subentities]
                    is_truncated = True
                    break

                if page_idx + 1 >= total_pages or not items:
                    break

                page_idx += 1

            except RequestBudgetExceededError:
                if page_idx == 0:
                    raise
                is_truncated = True
                break
            except requests.RequestException as e:
                if page_idx == 0:
                    return SubentitiesFetchResult(
                        status=SUBENTITIES_STATUS_FAILED,
                        error=f"Network error: {e}",
                        source_url=url,
                    )
                break
            except Exception as e:
                if page_idx == 0:
                    return SubentitiesFetchResult(
                        status=SUBENTITIES_STATUS_FAILED,
                        error=f"Unexpected error: {e}",
                        source_url=url,
                    )
                break

        if len(collected_subentities) < total_elements and not is_truncated:
            is_truncated = True

        if not collected_subentities:
            status = SUBENTITIES_STATUS_EMPTY
        elif is_truncated:
            status = SUBENTITIES_STATUS_TRUNCATED
        else:
            status = SUBENTITIES_STATUS_SUCCESS

        return SubentitiesFetchResult(
            status=status,
            http_status=200,
            subentities=collected_subentities,
            total_elements=total_elements,
            pages_fetched=pages_fetched,
            is_truncated=is_truncated,
            source_url=base_url,
        )


def extract_subentity_facts_and_evidence(
    subentities: List[Dict[str, Any]],
    parent_org_number: str,
    source_url: str,
    retrieved_at: Optional[str] = None,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Extract normalized child-scoped facts and parent-level summary facts for operating units.
    Returns list of (fact_key, fact_value, evidence_dict).
    """
    valid_parent_org = BrregClient.validate_org_number(parent_org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()

    raw_ev_str = json.dumps({"parent_org": valid_parent_org, "underenheter": subentities}, ensure_ascii=False, sort_keys=True)

    evidence_dict = {
        "exact_source_url": source_url,
        "retrieved_at": now_iso,
        "source_validity_date": None,
        "raw_evidence": raw_ev_str,
    }

    facts: List[Tuple[str, str, Dict[str, Any]]] = []

    # 1. Parent-level summary facts
    facts.append(("subentity_count", str(len(subentities)), evidence_dict))

    locations = set()

    for item in subentities:
        child_org = item.get("organisasjonsnummer")
        if not child_org:
            continue
        child_org = str(child_org).strip()
        prefix = f"subentity_{child_org}_"

        name = item.get("navn")
        if name:
            facts.append((f"{prefix}name", name.strip(), evidence_dict))

        # Org Form
        org_form = item.get("organisasjonsform") or {}
        form_code = org_form.get("kode")
        if form_code:
            facts.append((f"{prefix}org_form", form_code, evidence_dict))

        # Location / Address (strictly child-scoped)
        b_addr = item.get("beliggenhetsadresse") or {}
        if b_addr:
            street = ", ".join(b_addr.get("adresse") or [])
            post_code = b_addr.get("postnummer") or ""
            city = b_addr.get("poststed") or ""
            municipality = b_addr.get("kommune") or ""
            if municipality:
                locations.add(municipality)
            addr_str = json.dumps({
                "street": street,
                "postcode": post_code,
                "city": city,
                "municipality": municipality,
            }, ensure_ascii=False, sort_keys=True)
            facts.append((f"{prefix}location_address", addr_str, evidence_dict))

        # Industry
        ind1 = item.get("naeringskode1") or {}
        if ind1.get("kode"):
            ind_val = json.dumps({
                "code": ind1.get("kode"),
                "description": ind1.get("beskrivelse"),
            }, ensure_ascii=False, sort_keys=True)
            facts.append((f"{prefix}industry", ind_val, evidence_dict))

        # Employee count semantics (Requirement 8)
        ansatte = item.get("antallAnsatte")
        has_registered_ansatte = item.get("harRegistrertAntallAnsatte")

        if ansatte is not None:
            facts.append((f"{prefix}employee_count", str(ansatte), evidence_dict))
        elif has_registered_ansatte is True:
            # Respect missing != zero: record registered boolean indicator without inventing exact 0
            facts.append((f"{prefix}employee_registered", "true", evidence_dict))

        # Startup & Closure / Deletion dates
        if item.get("oppstartsdato"):
            facts.append((f"{prefix}startup_date", item.get("oppstartsdato"), evidence_dict))
        if item.get("nedleggelsesdato"):
            facts.append((f"{prefix}closure_date", item.get("nedleggelsesdato"), evidence_dict))
        if item.get("slettedato"):
            facts.append((f"{prefix}deletion_date", item.get("slettedato"), evidence_dict))

        # Website
        if item.get("hjemmeside"):
            facts.append((f"{prefix}website", item.get("hjemmeside"), evidence_dict))

    if locations:
        facts.append(("subentity_locations", json.dumps(sorted(list(locations)), ensure_ascii=False), evidence_dict))

    return facts


def save_subentity_facts_to_storage(
    conn: sqlite3.Connection,
    parent_org_number: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist subentity operating unit facts for one parent company into SQLite.
    """
    valid_parent_org = BrregClient.validate_org_number(parent_org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, 'https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}', 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (SUBENTITIES_SOURCE_ID, SUBENTITIES_SOURCE_NAME))

        existing = {
            row["fact_key"]: dict(row)
            for row in conn.execute("""
                SELECT fact_id, fact_key, fact_value FROM facts
                WHERE org_number = ? AND is_active = 1 AND (fact_key LIKE 'subentity_%' OR fact_key = 'subentity_count' OR fact_key = 'subentity_locations');
            """, (valid_parent_org,)).fetchall()
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
                        valid_parent_org,
                        old_key,
                        old["fact_value"],
                        now_iso,
                        "Subentity fact absent from complete current underenheter response. No reason inferred.",
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
                """, (valid_parent_org, key, value, now_iso, now_iso))
                fact_id = cur.lastrowid
                c_type = "updated" if old else "created"
                conn.execute("""
                    INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    valid_parent_org,
                    key,
                    old["fact_value"] if old else None,
                    value,
                    c_type,
                    now_iso,
                    "Subentity operating unit updated." if old else "First observation of subentity operating unit.",
                ))
                counts[c_type] += 1

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (
                fact_id,
                SUBENTITIES_SOURCE_ID,
                ev["exact_source_url"],
                ev["retrieved_at"],
                ev.get("source_validity_date"),
                ev.get("raw_evidence"),
            ))

    return counts
