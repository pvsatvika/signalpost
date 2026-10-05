"""
Safe company profile refresh, change detection, and structured change explanation module.
"""

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from signal_post.client import BrregClient
from signal_post.exceptions import SignalpostError
from signal_post.models import NormalizedCompanyProfile
from signal_post.storage import (
    DEFAULT_SOURCE_ID,
    DEFAULT_SOURCE_NAME,
    extract_facts,
    get_company,
    get_evidence_for_fact,
    get_facts,
    init_db,
    save_company_profile,
)


@dataclass
class FactChangeItem:
    """Represents a single fact observation, update, conflict, omission, or unchanged state."""
    org_number: str
    company_name: str
    fact_key: str
    previous_value: Optional[str]
    new_value: Optional[str]
    change_type: str  # 'created', 'updated', 'reasserted', 'conflict', 'unchanged', 'omitted'
    source_id: str
    source_name: str
    exact_source_url: str
    retrieved_at: str
    source_validity_date: Optional[str]
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_number": self.org_number,
            "company_name": self.company_name,
            "fact_key": self.fact_key,
            "previous_value": self.previous_value,
            "new_value": self.new_value,
            "change_type": self.change_type,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "exact_source_url": self.exact_source_url,
            "retrieved_at": self.retrieved_at,
            "source_validity_date": self.source_validity_date,
            "explanation": self.explanation,
        }


@dataclass
class RefreshResult:
    """Structured report returned after refreshing a company profile."""
    org_number: str
    company_name: Optional[str]
    status: str  # 'success' or 'failed'
    retrieved_at: str
    source_id: str
    source_name: str
    exact_source_url: str
    changes: List[FactChangeItem] = field(default_factory=list)
    summary_counts: Dict[str, int] = field(default_factory=dict)
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "org_number": self.org_number,
            "company_name": self.company_name,
            "status": self.status,
            "retrieved_at": self.retrieved_at,
            "source_id": self.source_id,
            "source_name": self.source_name,
            "exact_source_url": self.exact_source_url,
            "summary_counts": self.summary_counts,
            "changes": [c.to_dict() for c in self.changes],
            "error_message": self.error_message,
        }


def format_explanation(
    fact_key: str,
    change_type: str,
    previous_value: Optional[str],
    new_value: Optional[str],
    source_name: str,
    source_id: str,
) -> str:
    """Generate a strictly factual, non-speculative explanation for a detected change."""
    if change_type == "created":
        return f"Fact '{fact_key}' was observed for the first time as '{new_value}' from '{source_name}'."
    elif change_type == "updated":
        return f"Fact '{fact_key}' changed from '{previous_value}' to '{new_value}' according to the latest record from '{source_name}'."
    elif change_type == "reasserted":
        return f"Previously inactive fact '{fact_key}' was re-observed as '{new_value}' from '{source_name}'."
    elif change_type == "conflict":
        return f"Conflicting value '{new_value}' for fact '{fact_key}' reported by source '{source_id}' vs existing stored value '{previous_value}'."
    elif change_type == "omitted":
        return f"Fact '{fact_key}' (previously '{previous_value}') was omitted in the latest response from '{source_name}'; existing stored fact preserved."
    elif change_type == "unchanged":
        return f"Fact '{fact_key}' remains unchanged at '{new_value}'."
    else:
        return f"Fact '{fact_key}' recorded change type '{change_type}' from '{source_name}'."


def refresh_company(
    conn: sqlite3.Connection,
    org_number: str,
    client: Optional[BrregClient] = None,
    budget_tracker: Optional[Any] = None,
    source_id: str = DEFAULT_SOURCE_ID,
    source_name: str = DEFAULT_SOURCE_NAME,
    source_validity_date: Optional[str] = None,
) -> RefreshResult:
    """
    Safely refresh a company profile by fetching the latest registry record, validating identity,
    comparing against stored facts, committing safe updates to SQLite, and generating a structured change report.

    :param conn: Active SQLite database connection.
    :param org_number: Norwegian organization number.
    :param client: Optional custom or mocked BrregClient.
    :param budget_tracker: Optional RequestBudgetTracker instance for request budget enforcement.
    :param source_id: Data source identifier.
    :param source_name: Human-readable data source name.
    :param source_validity_date: Optional publication/validity date from source.
    :return: RefreshResult containing status, summary counts, and change explanations.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    init_db(conn)

    # 1. Validate organization number format before network call
    try:
        valid_org = BrregClient.validate_org_number(org_number)
    except SignalpostError as e:
        return RefreshResult(
            org_number=str(org_number),
            company_name=None,
            status="failed",
            retrieved_at=now_iso,
            source_id=source_id,
            source_name=source_name,
            exact_source_url=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}",
            error_message=str(e),
            summary_counts={"error": 1},
        )

    exact_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{valid_org}"
    active_client = client or BrregClient(budget_tracker=budget_tracker)
    if budget_tracker and getattr(active_client, "budget_tracker", None) is None:
        active_client.budget_tracker = budget_tracker

    # 2. Fetch and normalize company profile (validates identity & format)
    try:
        profile = active_client.get_company_profile(valid_org)
    except SignalpostError as e:
        existing_comp = get_company(conn, valid_org)
        comp_name = existing_comp["name"] if existing_comp else None
        return RefreshResult(
            org_number=valid_org,
            company_name=comp_name,
            status="failed",
            retrieved_at=now_iso,
            source_id=source_id,
            source_name=source_name,
            exact_source_url=exact_url,
            error_message=str(e),
            summary_counts={"error": 1},
        )
    except Exception as e:
        existing_comp = get_company(conn, valid_org)
        comp_name = existing_comp["name"] if existing_comp else None
        return RefreshResult(
            org_number=valid_org,
            company_name=comp_name,
            status="failed",
            retrieved_at=now_iso,
            source_id=source_id,
            source_name=source_name,
            exact_source_url=exact_url,
            error_message=f"Unexpected error during fetch: {e}",
            summary_counts={"error": 1},
        )

    # 3. Retrieve currently stored active facts for change comparison
    stored_active_facts = {f["fact_key"]: f for f in get_facts(conn, valid_org, active_only=True)}
    latest_extracted_facts = dict(extract_facts(profile))

    change_items: List[FactChangeItem] = []
    summary_counts: Dict[str, int] = {
        "created": 0,
        "updated": 0,
        "reasserted": 0,
        "conflict": 0,
        "unchanged": 0,
        "omitted": 0,
    }

    # 4. Compare latest facts against stored active facts
    for fact_key, new_val in latest_extracted_facts.items():
        if fact_key not in stored_active_facts:
            cur_prev = conn.execute("""
                SELECT fact_value FROM facts
                WHERE org_number = ? AND fact_key = ?
                ORDER BY fact_id DESC LIMIT 1;
            """, (valid_org, fact_key))
            prev_row = cur_prev.fetchone()
            prev_val = prev_row["fact_value"] if prev_row else None
            c_type = "reasserted" if (prev_val is not None and prev_val == new_val) else "created"

            explanation = format_explanation(fact_key, c_type, prev_val, new_val, source_name, source_id)
            change_items.append(FactChangeItem(
                org_number=valid_org,
                company_name=profile.name,
                fact_key=fact_key,
                previous_value=prev_val,
                new_value=new_val,
                change_type=c_type,
                source_id=source_id,
                source_name=source_name,
                exact_source_url=exact_url,
                retrieved_at=now_iso,
                source_validity_date=source_validity_date,
                explanation=explanation,
            ))
            summary_counts[c_type] += 1

        else:
            stored_fact = stored_active_facts[fact_key]
            stored_val = stored_fact["fact_value"]

            if stored_val == new_val:
                c_type = "unchanged"
                explanation = format_explanation(fact_key, c_type, stored_val, new_val, source_name, source_id)
                change_items.append(FactChangeItem(
                    org_number=valid_org,
                    company_name=profile.name,
                    fact_key=fact_key,
                    previous_value=stored_val,
                    new_value=new_val,
                    change_type=c_type,
                    source_id=source_id,
                    source_name=source_name,
                    exact_source_url=exact_url,
                    retrieved_at=now_iso,
                    source_validity_date=source_validity_date,
                    explanation=explanation,
                ))
                summary_counts[c_type] += 1
            else:
                # Value differs. Check primary source provenance (the first source that established this fact)
                cur_primary = conn.execute("""
                    SELECT source_id FROM evidence
                    WHERE fact_id = ?
                    ORDER BY evidence_id ASC LIMIT 1;
                """, (stored_fact["fact_id"],))
                primary_row = cur_primary.fetchone()
                primary_source_id = primary_row["source_id"] if primary_row else None

                c_type = "updated" if primary_source_id == source_id else "conflict"
                explanation = format_explanation(fact_key, c_type, stored_val, new_val, source_name, source_id)
                change_items.append(FactChangeItem(
                    org_number=valid_org,
                    company_name=profile.name,
                    fact_key=fact_key,
                    previous_value=stored_val,
                    new_value=new_val,
                    change_type=c_type,
                    source_id=source_id,
                    source_name=source_name,
                    exact_source_url=exact_url,
                    retrieved_at=now_iso,
                    source_validity_date=source_validity_date,
                    explanation=explanation,
                ))
                summary_counts[c_type] += 1

    # 5. Check for stored active facts omitted in latest response
    for fact_key, stored_fact in stored_active_facts.items():
        if fact_key not in latest_extracted_facts:
            c_type = "omitted"
            stored_val = stored_fact["fact_value"]
            explanation = format_explanation(fact_key, c_type, stored_val, None, source_name, source_id)
            change_items.append(FactChangeItem(
                org_number=valid_org,
                company_name=profile.name,
                fact_key=fact_key,
                previous_value=stored_val,
                new_value=None,
                change_type=c_type,
                source_id=source_id,
                source_name=source_name,
                exact_source_url=exact_url,
                retrieved_at=now_iso,
                source_validity_date=source_validity_date,
                explanation=explanation,
            ))
            summary_counts[c_type] += 1

    # 6. Commit valid updates to SQLite database atomically inside transaction
    try:
        save_company_profile(
            conn=conn,
            profile=profile,
            source_id=source_id,
            exact_source_url=exact_url,
            retrieved_at=now_iso,
            source_validity_date=source_validity_date,
        )
    except Exception as e:
        return RefreshResult(
            org_number=valid_org,
            company_name=profile.name,
            status="failed",
            retrieved_at=now_iso,
            source_id=source_id,
            source_name=source_name,
            exact_source_url=exact_url,
            error_message=f"Database transaction commit failed: {e}",
            summary_counts={"error": 1},
        )

    return RefreshResult(
        org_number=valid_org,
        company_name=profile.name,
        status="success",
        retrieved_at=now_iso,
        source_id=source_id,
        source_name=source_name,
        exact_source_url=exact_url,
        changes=change_items,
        summary_counts=summary_counts,
    )
