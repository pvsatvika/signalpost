"""
Official Brønnøysund Corporate Group Structure (Konsernstruktur) API integration for Signalpost.

Endpoint (open, no authentication required):
    GET https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org_number}

Responsibilities:
- Budget-enforced, identity-verified retrieval of corporate group hierarchies.
- Strict relationship scoping: corporate group member facts are relationship-scoped
  (`group_relation_<parent>_<child>_<code...`), and NEVER merged into parent legal entity direct attributes.
- Safe recursive tree traversal with cycle detection and bounded max-depth / max-node caps.
- Conservative status semantics: distinguish success, no_group_returned, failed, malformed, budget_exhausted.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.storage import init_db

logger = logging.getLogger(__name__)

GROUP_STRUCTURE_SOURCE_ID = "brreg_group_structure"
GROUP_STRUCTURE_SOURCE_NAME = "Brønnøysund Register Centre - Corporate Group Structure API"
GROUP_STRUCTURE_URL_TEMPLATE = "https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}"

# Outcome status categories
GROUP_STATUS_SUCCESS = "success"            # Identity-verified response with corporate group structure
GROUP_STATUS_NO_GROUP = "no_group_returned"  # HTTP 404 (company is not in a registered corporate group)
GROUP_STATUS_FAILED = "failed"              # Network error, timeout, HTTP error
GROUP_STATUS_MALFORMED = "malformed"        # Invalid JSON or unexpected schema
GROUP_STATUS_BUDGET = "budget_exhausted"    # Request blocked before transmission


@dataclass
class GroupStructureFetchResult:
    status: str
    http_status: Optional[int] = None
    root_org_number: Optional[str] = None
    root_name: Optional[str] = None
    root_payload: Optional[Dict[str, Any]] = None
    nodes_count: int = 0
    error: Optional[str] = None
    source_url: str = ""
    retrieved_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def is_complete_set(self) -> bool:
        """True for successful or no-group responses where structure was checked."""
        return self.status in (GROUP_STATUS_SUCCESS, GROUP_STATUS_NO_GROUP)


class BrregGroupClient:
    """
    Budget-enforced client for official Brønnøysund Corporate Group Structure API.
    """

    def __init__(
        self,
        client: Optional[BrregClient] = None,
        session: Optional[requests.Session] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        timeout: float = 10.0,
        max_nodes: int = 500,
        max_depth: int = 20,
    ):
        self.client = client or BrregClient()
        self.session = session or self.client.session
        self.budget_tracker = budget_tracker
        self.timeout = timeout
        self.max_nodes = max_nodes
        self.max_depth = max_depth

    def fetch_group_structure(self, org_number: str) -> GroupStructureFetchResult:
        """
        Query Brønnøysund corporate group structure endpoint for a 9-digit org number.
        Returns hierarchy rooted at ultimate parent company. Never raises for HTTP 404 / network errors.
        """
        valid_org = BrregClient.validate_org_number(org_number)
        url = f"https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{valid_org}"

        if self.budget_tracker and not self.budget_tracker.can_make_request():
            raise RequestBudgetExceededError(
                f"Cannot make outbound corporate group request to {url}: budget limit exceeded."
            )

        try:
            if self.budget_tracker:
                self.budget_tracker.record_request("group_structure_fetch", url)

            resp = self.session.get(url, timeout=self.timeout, headers={"Accept": "application/json"})
            http_code = resp.status_code

            if http_code == 404:
                return GroupStructureFetchResult(
                    status=GROUP_STATUS_NO_GROUP,
                    http_status=404,
                    source_url=url,
                )

            if http_code != 200:
                return GroupStructureFetchResult(
                    status=GROUP_STATUS_FAILED,
                    http_status=http_code,
                    error=f"HTTP status {http_code}",
                    source_url=url,
                )

            try:
                data = resp.json()
            except Exception as e:
                return GroupStructureFetchResult(
                    status=GROUP_STATUS_MALFORMED,
                    http_status=200,
                    error=f"JSON decode failure: {e}",
                    source_url=url,
                )

            if not isinstance(data, dict):
                return GroupStructureFetchResult(
                    status=GROUP_STATUS_MALFORMED,
                    http_status=200,
                    error="Expected top-level JSON object in group structure response.",
                    source_url=url,
                )

            root_org = str(data.get("organisasjonsnummer") or "").strip()
            root_name = data.get("navn")

            # Quick node count using set
            visited: Set[str] = set()
            self._count_nodes(data, visited, depth=0)

            return GroupStructureFetchResult(
                status=GROUP_STATUS_SUCCESS,
                http_status=200,
                root_org_number=root_org if root_org else None,
                root_name=root_name,
                root_payload=data,
                nodes_count=len(visited),
                source_url=url,
            )

        except RequestBudgetExceededError:
            raise
        except requests.RequestException as e:
            return GroupStructureFetchResult(
                status=GROUP_STATUS_FAILED,
                error=f"Network error: {e}",
                source_url=url,
            )
        except Exception as e:
            return GroupStructureFetchResult(
                status=GROUP_STATUS_FAILED,
                error=f"Unexpected error: {e}",
                source_url=url,
            )

    def _count_nodes(self, node: Dict[str, Any], visited: Set[str], depth: int) -> None:
        if depth > self.max_depth or len(visited) >= self.max_nodes:
            return
        org = str(node.get("organisasjonsnummer") or "").strip()
        if org and org not in visited:
            visited.add(org)
            children = node.get("children") or []
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, dict):
                        self._count_nodes(child, visited, depth + 1)


def extract_group_facts_and_evidence(
    root_payload: Optional[Dict[str, Any]],
    queried_org_number: str,
    source_url: str,
    retrieved_at: Optional[str] = None,
    max_nodes: int = 500,
    max_depth: int = 20,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    """
    Extract normalized relationship facts and group summary facts for a target company profile.
    Returns list of (fact_key, fact_value, evidence_dict).
    """
    valid_queried_org = BrregClient.validate_org_number(queried_org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()

    if not root_payload or not isinstance(root_payload, dict):
        return []

    try:
        raw_ev_str = json.dumps({"queried_org": valid_queried_org, "root_payload": root_payload}, ensure_ascii=False, sort_keys=True)
    except (ValueError, TypeError):
        raw_ev_str = json.dumps({"queried_org": valid_queried_org, "root_payload": str(root_payload)}, ensure_ascii=False, sort_keys=True)

    evidence_dict = {
        "exact_source_url": source_url,
        "retrieved_at": now_iso,
        "source_validity_date": None,
        "raw_evidence": raw_ev_str,
    }

    facts: List[Tuple[str, str, Dict[str, Any]]] = []

    root_org = str(root_payload.get("organisasjonsnummer") or "").strip()
    root_name = root_payload.get("navn") or ""

    visited_orgs: Set[str] = set()
    relationships: List[Dict[str, Any]] = []

    def _traverse(node: Dict[str, Any], current_depth: int) -> None:
        if current_depth > max_depth or len(visited_orgs) >= max_nodes:
            logger.warning(f"Group structure traversal safety cap reached for {valid_queried_org} (depth={current_depth}, nodes={len(visited_orgs)})")
            return
        node_org = str(node.get("organisasjonsnummer") or "").strip()
        if not node_org or node_org in visited_orgs:
            return
        visited_orgs.add(node_org)

        children = node.get("children") or []
        if not isinstance(children, list):
            return

        for child in children:
            if not isinstance(child, dict):
                continue
            child_org = str(child.get("organisasjonsnummer") or "").strip()
            if not child_org:
                continue

            parent_org = str(child.get("parentOrganisasjonsnummer") or node_org).strip()
            parent_name = child.get("parentNavn") or node.get("navn") or ""

            knytningsform = child.get("knytningsform") or {}
            rel_code = knytningsform.get("kode") or ""
            rel_desc = knytningsform.get("beskrivelse") or ""
            basis = child.get("grunnlag") or ""
            effective_date = child.get("dato") or ""
            level = child.get("nivaa")
            child_name = child.get("navn") or ""
            child_org_form_dict = child.get("organisasjonsform") or {}
            child_org_form = child_org_form_dict.get("kode") or ""

            rel_item = {
                "parent_org": parent_org,
                "parent_name": parent_name,
                "child_org": child_org,
                "child_name": child_name,
                "child_org_form": child_org_form,
                "relationship_code": rel_code,
                "relationship_description": rel_desc,
                "ownership_basis": basis,
                "effective_date": effective_date,
                "level": level,
            }
            relationships.append(rel_item)

            _traverse(child, current_depth + 1)

    _traverse(root_payload, 0)

    # 1. Target Org Summary Facts
    facts.append(("is_in_registered_group", "true", evidence_dict))
    if root_org:
        facts.append(("registered_group_root_org", root_org, evidence_dict))
    if root_name:
        facts.append(("registered_group_root_name", root_name, evidence_dict))
    facts.append(("registered_group_node_count", str(len(visited_orgs)), evidence_dict))

    # 2. Relationship Facts
    for rel in relationships:
        p_org = rel["parent_org"]
        c_org = rel["child_org"]
        code = rel["relationship_code"] or "REL"
        fact_key = f"group_relation_{p_org}_{c_org}_{code}"
        fact_val = json.dumps(rel, ensure_ascii=False, sort_keys=True)
        facts.append((fact_key, fact_val, evidence_dict))

    return facts


def save_group_facts_to_storage(
    conn: sqlite3.Connection,
    queried_org_number: str,
    extracted_facts: List[Tuple[str, str, Dict[str, Any]]],
    retrieved_at: Optional[str] = None,
    complete_set: bool = True,
) -> Dict[str, int]:
    """
    Atomically persist corporate group structure facts for one target company into SQLite.
    """
    valid_queried_org = BrregClient.validate_org_number(queried_org_number)
    now_iso = retrieved_at or datetime.now(timezone.utc).isoformat()
    init_db(conn)
    counts = {"created": 0, "unchanged": 0, "updated": 0, "removed": 0}

    with conn:
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, 'https://data.brreg.no/enhetsregisteret/api/konsernstruktur/{org}', 'official_public_api')
            ON CONFLICT(source_id) DO UPDATE SET name=excluded.name;
        """, (GROUP_STRUCTURE_SOURCE_ID, GROUP_STRUCTURE_SOURCE_NAME))

        existing = {
            row["fact_key"]: dict(row)
            for row in conn.execute("""
                SELECT fact_id, fact_key, fact_value FROM facts
                WHERE org_number = ? AND is_active = 1 AND (fact_key LIKE 'group_%' OR fact_key = 'is_in_registered_group' OR fact_key LIKE 'registered_group_%');
            """, (valid_queried_org,)).fetchall()
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
                        valid_queried_org,
                        old_key,
                        old["fact_value"],
                        now_iso,
                        "Corporate group fact absent from complete current group structure response. No reason inferred.",
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
                """, (valid_queried_org, key, value, now_iso, now_iso))
                fact_id = cur.lastrowid
                c_type = "updated" if old else "created"
                conn.execute("""
                    INSERT INTO change_history (org_number, fact_key, previous_value, new_value, change_type, changed_at, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (
                    valid_queried_org,
                    key,
                    old["fact_value"] if old else None,
                    value,
                    c_type,
                    now_iso,
                    "Corporate group structure updated." if old else "First observation of corporate group structure.",
                ))
                counts[c_type] += 1

            raw_ev = ev.get("raw_evidence")
            if raw_ev is None:
                raw_ev = json.dumps(ev, ensure_ascii=False, sort_keys=True)

            conn.execute("""
                INSERT INTO evidence (fact_id, source_id, exact_source_url, retrieved_at, source_validity_date, raw_evidence)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (
                fact_id,
                GROUP_STRUCTURE_SOURCE_ID,
                ev.get("exact_source_url", "https://data.brreg.no/enhetsregisteret/api/konsernstruktur"),
                ev.get("retrieved_at", now_iso),
                ev.get("source_validity_date"),
                raw_ev,
            ))

    return counts
