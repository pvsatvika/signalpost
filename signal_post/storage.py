"""
SQLite storage module for Signalpost company profiles, facts, evidence, and change history.
"""

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from signal_post.client import BrregClient
from signal_post.models import NormalizedCompanyProfile
from signal_post.exceptions import SignalpostError

DEFAULT_SOURCE_ID = "brreg_enhetsregisteret"
DEFAULT_SOURCE_NAME = "Brønnøysundregistrene - Enhetsregisteret"
DEFAULT_SOURCE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"
DEFAULT_SOURCE_TYPE = "official_public_api"

BULK_SOURCE_ID = "brreg_bulk_enhetsregisteret"
BULK_SOURCE_NAME = "Brønnøysundregistrene - Enhetsregisteret (Bulk Open Data)"
BULK_SOURCE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned"
BULK_SOURCE_TYPE = "official_public_bulk_dataset"


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Create a SQLite connection with foreign keys enabled and row factory configured.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """
    Initialize SQLite schema for Signalpost.
    """
    conn.row_factory = sqlite3.Row
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS companies (
                org_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                organization_form_code TEXT,
                organization_form_name TEXT,
                primary_industry_code TEXT,
                primary_industry_name TEXT,
                num_employees INTEGER,
                registration_date TEXT,
                first_seen_at TEXT NOT NULL,
                last_checked_at TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                source_type TEXT NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                fact_id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_number TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                fact_value TEXT NOT NULL,
                verification_status TEXT NOT NULL DEFAULT 'source_asserted',
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (org_number) REFERENCES companies (org_number) ON DELETE CASCADE
            );
        """)

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_active_key 
            ON facts (org_number, fact_key) WHERE is_active = 1;
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                fact_id INTEGER NOT NULL,
                source_id TEXT NOT NULL,
                exact_source_url TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                source_validity_date TEXT,
                raw_evidence TEXT NOT NULL,
                metadata_json TEXT,
                FOREIGN KEY (fact_id) REFERENCES facts (fact_id) ON DELETE CASCADE,
                FOREIGN KEY (source_id) REFERENCES sources (source_id)
            );
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_evidence_fact_id ON evidence (fact_id);
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS change_history (
                change_id INTEGER PRIMARY KEY AUTOINCREMENT,
                org_number TEXT NOT NULL,
                fact_key TEXT NOT NULL,
                previous_value TEXT,
                new_value TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                change_type TEXT NOT NULL,
                reason TEXT,
                FOREIGN KEY (org_number) REFERENCES companies (org_number) ON DELETE CASCADE
            );
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_change_history_org ON change_history (org_number);
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS collection_queue (
                org_number TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                organization_form_code TEXT,
                registration_date TEXT,
                discovered_at TEXT NOT NULL,
                discovery_source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                error_message TEXT
            );
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_collection_queue_status ON collection_queue (status);
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS request_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_type TEXT NOT NULL,
                target_url TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
        """)

        # Ensure default source exists
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                name = excluded.name,
                source_url = excluded.source_url,
                source_type = excluded.source_type;
        """, (DEFAULT_SOURCE_ID, DEFAULT_SOURCE_NAME, DEFAULT_SOURCE_URL, DEFAULT_SOURCE_TYPE))

        # Ensure bulk open data source exists
        conn.execute("""
            INSERT INTO sources (source_id, name, source_url, source_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                name = excluded.name,
                source_url = excluded.source_url,
                source_type = excluded.source_type;
        """, (BULK_SOURCE_ID, BULK_SOURCE_NAME, BULK_SOURCE_URL, BULK_SOURCE_TYPE))


def extract_facts(profile: NormalizedCompanyProfile) -> List[Tuple[str, str]]:
    """
    Extract key-value fact pairs from a NormalizedCompanyProfile.
    Only non-None attributes are extracted as factual assertions.
    Serializes JSON with deterministic key ordering (`sort_keys=True`) and strips string whitespace.
    """
    facts: List[Tuple[str, str]] = []

    if profile.name:
        facts.append(("name", profile.name.strip()))

    if profile.organization_form:
        facts.append(("organization_form", json.dumps({
            "code": profile.organization_form.kode,
            "description": profile.organization_form.beskrivelse
        }, ensure_ascii=False, sort_keys=True)))

    if profile.primary_industry:
        facts.append(("primary_industry", json.dumps({
            "code": profile.primary_industry.kode,
            "description": profile.primary_industry.beskrivelse
        }, ensure_ascii=False, sort_keys=True)))

    if profile.secondary_industry:
        facts.append(("secondary_industry", json.dumps({
            "code": profile.secondary_industry.kode,
            "description": profile.secondary_industry.beskrivelse
        }, ensure_ascii=False, sort_keys=True)))

    if profile.tertiary_industry:
        facts.append(("tertiary_industry", json.dumps({
            "code": profile.tertiary_industry.kode,
            "description": profile.tertiary_industry.beskrivelse
        }, ensure_ascii=False, sort_keys=True)))

    if profile.num_employees is not None:
        facts.append(("num_employees", str(profile.num_employees)))

    if profile.has_registered_employees is not None:
        facts.append(("has_registered_employees", str(profile.has_registered_employees)))

    if profile.registration_date:
        facts.append(("registration_date", profile.registration_date.strip()))

    if profile.foundation_date:
        facts.append(("foundation_date", profile.foundation_date.strip()))

    if profile.parent_org_number:
        facts.append(("parent_org_number", profile.parent_org_number.strip()))

    if profile.business_address:
        facts.append(("business_address", json.dumps(profile.business_address.model_dump(), ensure_ascii=False, sort_keys=True)))

    if profile.postal_address:
        facts.append(("postal_address", json.dumps(profile.postal_address.model_dump(), ensure_ascii=False, sort_keys=True)))

    if profile.website:
        facts.append(("website", profile.website.strip()))

    if profile.email:
        facts.append(("email", profile.email.strip()))

    if profile.phone:
        facts.append(("phone", profile.phone.strip()))

    if profile.registered_in_foretaksregisteret is not None:
        facts.append(("registered_in_foretaksregisteret", str(profile.registered_in_foretaksregisteret)))

    if profile.registered_in_mvaregisteret is not None:
        facts.append(("registered_in_mvaregisteret", str(profile.registered_in_mvaregisteret)))

    if profile.registered_in_frivillighetsregisteret is not None:
        facts.append(("registered_in_frivillighetsregisteret", str(profile.registered_in_frivillighetsregisteret)))

    if profile.registered_in_stiftelsesregisteret is not None:
        facts.append(("registered_in_stiftelsesregisteret", str(profile.registered_in_stiftelsesregisteret)))

    if profile.registered_in_partiregisteret is not None:
        facts.append(("registered_in_partiregisteret", str(profile.registered_in_partiregisteret)))

    if profile.is_in_group is not None:
        facts.append(("is_in_group", str(profile.is_in_group)))

    if profile.under_liquidation is not None:
        facts.append(("under_liquidation", str(profile.under_liquidation)))

    if profile.under_compulsory_dissolution is not None:
        facts.append(("under_compulsory_dissolution", str(profile.under_compulsory_dissolution)))

    if profile.is_bankrupt is not None:
        facts.append(("is_bankrupt", str(profile.is_bankrupt)))

    if profile.last_submitted_annual_accounts:
        facts.append(("last_submitted_annual_accounts", profile.last_submitted_annual_accounts.strip()))

    if profile.capital:
        facts.append(("capital", json.dumps(profile.capital.model_dump(), ensure_ascii=False, sort_keys=True)))

    if profile.historical_names:
        facts.append(("historical_names", json.dumps([
            h.model_dump() for h in profile.historical_names
        ], ensure_ascii=False, sort_keys=True)))

    return facts


def save_company_profile(
    conn: sqlite3.Connection,
    profile: NormalizedCompanyProfile,
    source_id: str = DEFAULT_SOURCE_ID,
    exact_source_url: Optional[str] = None,
    retrieved_at: Optional[str] = None,
    source_validity_date: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Save or update a company profile, its individual facts, evidence traces, and change history.
    Executes atomically inside a single transaction context.
    """
    # 0. Validate org number format to prevent corrupt database insertions
    valid_org = BrregClient.validate_org_number(profile.org_number)

    now_iso = datetime.now(timezone.utc).isoformat()
    op_timestamp = retrieved_at or now_iso
    url = exact_source_url or f"https://data.brreg.no/enhetsregisteret/api/enheter/{valid_org}"
    raw_json_str = json.dumps(profile.raw_data, ensure_ascii=False, sort_keys=True)
    meta_json_str = json.dumps(metadata, ensure_ascii=False, sort_keys=True) if metadata else None

    org_form_code = profile.organization_form.kode if profile.organization_form else None
    org_form_name = profile.organization_form.beskrivelse if profile.organization_form else None
    ind_code = profile.primary_industry.kode if profile.primary_industry else None
    ind_name = profile.primary_industry.beskrivelse if profile.primary_industry else None

    with conn:
        # Ensure source exists in sources table
        cur_src = conn.execute("SELECT source_id FROM sources WHERE source_id = ?;", (source_id,))
        if not cur_src.fetchone():
            conn.execute("""
                INSERT INTO sources (source_id, name, source_url, source_type)
                VALUES (?, ?, ?, ?);
            """, (source_id, f"Source '{source_id}'", exact_source_url or "unknown", "external_source"))

        # 1. Update or Insert company profile record
        cur = conn.execute("SELECT org_number, first_seen_at FROM companies WHERE org_number = ?;", (valid_org,))
        existing_comp = cur.fetchone()

        if existing_comp:
            conn.execute("""
                UPDATE companies SET
                    name = ?,
                    organization_form_code = ?,
                    organization_form_name = ?,
                    primary_industry_code = ?,
                    primary_industry_name = ?,
                    num_employees = ?,
                    registration_date = ?,
                    last_checked_at = ?
                WHERE org_number = ?;
            """, (
                profile.name,
                org_form_code,
                org_form_name,
                ind_code,
                ind_name,
                profile.num_employees,
                profile.registration_date,
                op_timestamp,
                valid_org,
            ))
        else:
            conn.execute("""
                INSERT INTO companies (
                    org_number, name, organization_form_code, organization_form_name,
                    primary_industry_code, primary_industry_name, num_employees,
                    registration_date, first_seen_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                valid_org,
                profile.name,
                org_form_code,
                org_form_name,
                ind_code,
                ind_name,
                profile.num_employees,
                profile.registration_date,
                op_timestamp,
                op_timestamp,
            ))

        # 2. Extract facts and update fact table + evidence + change history
        facts = extract_facts(profile)

        for fact_key, new_val in facts:
            cur = conn.execute("""
                SELECT fact_id, fact_value, verification_status FROM facts
                WHERE org_number = ? AND fact_key = ? AND is_active = 1;
            """, (valid_org, fact_key))
            active_row = cur.fetchone()

            if active_row is None:
                # No active fact currently exists. Check if there was a previous inactive fact for change history context
                cur_prev = conn.execute("""
                    SELECT fact_value FROM facts
                    WHERE org_number = ? AND fact_key = ?
                    ORDER BY fact_id DESC LIMIT 1;
                """, (valid_org, fact_key))
                prev_row = cur_prev.fetchone()
                prev_val = prev_row["fact_value"] if prev_row else None
                change_type = "reasserted" if (prev_val is not None and prev_val == new_val) else "created"

                cur_ins = conn.execute("""
                    INSERT INTO facts (
                        org_number, fact_key, fact_value, verification_status,
                        first_observed_at, last_observed_at, is_active
                    ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                """, (valid_org, fact_key, new_val, op_timestamp, op_timestamp))
                fact_id = cur_ins.lastrowid

                # Attach evidence
                conn.execute("""
                    INSERT INTO evidence (
                        fact_id, source_id, exact_source_url, retrieved_at,
                        source_validity_date, raw_evidence, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (fact_id, source_id, url, op_timestamp, source_validity_date, raw_json_str, meta_json_str))

                # Record change history entry
                reason_str = "Re-observed inactive fact" if change_type == "reasserted" else "Initial fact observation"
                conn.execute("""
                    INSERT INTO change_history (
                        org_number, fact_key, previous_value, new_value, changed_at, change_type, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (valid_org, fact_key, prev_val, new_val, op_timestamp, change_type, reason_str))

            elif active_row["fact_value"] == new_val:
                # Unchanged fact value
                fact_id = active_row["fact_id"]
                conn.execute("""
                    UPDATE facts SET last_observed_at = ? WHERE fact_id = ?;
                """, (op_timestamp, fact_id))

                # Check existing sources for this fact
                cur_sources = conn.execute("SELECT DISTINCT source_id FROM evidence WHERE fact_id = ?;", (fact_id,))
                existing_sources = {r["source_id"] for r in cur_sources.fetchall()}

                # If a DIFFERENT independent source also asserts this exact same value, upgrade status to 'corroborated'
                if source_id not in existing_sources and active_row["verification_status"] != "corroborated":
                    conn.execute("UPDATE facts SET verification_status = 'corroborated' WHERE fact_id = ?;", (fact_id,))

                # Attach new evidence entry
                conn.execute("""
                    INSERT INTO evidence (
                        fact_id, source_id, exact_source_url, retrieved_at,
                        source_validity_date, raw_evidence, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """, (fact_id, source_id, url, op_timestamp, source_validity_date, raw_json_str, meta_json_str))

            else:
                # Different fact value: check primary source provenance (the first source that established this fact)
                fact_id = active_row["fact_id"]
                old_val = active_row["fact_value"]

                cur_primary = conn.execute("""
                    SELECT source_id FROM evidence
                    WHERE fact_id = ?
                    ORDER BY evidence_id ASC LIMIT 1;
                """, (fact_id,))
                primary_row = cur_primary.fetchone()
                primary_source_id = primary_row["source_id"] if primary_row else None

                if primary_source_id == source_id:
                    # Same primary source reporting a new value chronologically -> update fact
                    conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (fact_id,))

                    cur_ins = conn.execute("""
                        INSERT INTO facts (
                            org_number, fact_key, fact_value, verification_status,
                            first_observed_at, last_observed_at, is_active
                        ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                    """, (valid_org, fact_key, new_val, op_timestamp, op_timestamp))
                    new_fact_id = cur_ins.lastrowid

                    conn.execute("""
                        INSERT INTO evidence (
                            fact_id, source_id, exact_source_url, retrieved_at,
                            source_validity_date, raw_evidence, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (new_fact_id, source_id, url, op_timestamp, source_validity_date, raw_json_str, meta_json_str))

                    conn.execute("""
                        INSERT INTO change_history (
                            org_number, fact_key, previous_value, new_value, changed_at, change_type, reason
                        ) VALUES (?, ?, ?, ?, ?, 'updated', ?);
                    """, (valid_org, fact_key, old_val, new_val, op_timestamp, f"Source update from '{source_id}'"))

                else:
                    # Different source reporting conflicting value -> flag conflict, preserve evidence, do NOT overwrite active fact
                    conn.execute("UPDATE facts SET verification_status = 'conflicting' WHERE fact_id = ?;", (fact_id,))

                    conn.execute("""
                        INSERT INTO evidence (
                            fact_id, source_id, exact_source_url, retrieved_at,
                            source_validity_date, raw_evidence, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (fact_id, source_id, url, op_timestamp, source_validity_date, raw_json_str, meta_json_str))

                    # Prevent duplicate conflict records for repeated queries from same conflicting source
                    cur_conflict = conn.execute("""
                        SELECT 1 FROM change_history
                        WHERE org_number = ? AND fact_key = ? AND change_type = 'conflict' AND new_value = ?;
                    """, (valid_org, fact_key, new_val))

                    if not cur_conflict.fetchone():
                        conn.execute("""
                            INSERT INTO change_history (
                                org_number, fact_key, previous_value, new_value, changed_at, change_type, reason
                            ) VALUES (?, ?, ?, ?, ?, 'conflict', ?);
                        """, (valid_org, fact_key, old_val, new_val, op_timestamp, f"Conflict: source '{source_id}' reported '{new_val}' vs existing '{old_val}'"))


def get_company(conn: sqlite3.Connection, org_number: str) -> Optional[Dict[str, Any]]:
    """Retrieve stored company record by organization number."""
    valid_org = BrregClient.validate_org_number(org_number)
    cur = conn.execute("SELECT * FROM companies WHERE org_number = ?;", (valid_org,))
    row = cur.fetchone()
    return dict(row) if row else None


def get_facts(conn: sqlite3.Connection, org_number: str, active_only: bool = True) -> List[Dict[str, Any]]:
    """Retrieve facts for a company."""
    valid_org = BrregClient.validate_org_number(org_number)
    if active_only:
        cur = conn.execute("SELECT * FROM facts WHERE org_number = ? AND is_active = 1 ORDER BY fact_key;", (valid_org,))
    else:
        cur = conn.execute("SELECT * FROM facts WHERE org_number = ? ORDER BY fact_id ASC;", (valid_org,))
    return [dict(r) for r in cur.fetchall()]


def get_evidence_for_fact(conn: sqlite3.Connection, fact_id: int) -> List[Dict[str, Any]]:
    """Retrieve all evidence records linked to a specific fact ID."""
    cur = conn.execute("SELECT * FROM evidence WHERE fact_id = ? ORDER BY evidence_id ASC;", (fact_id,))
    return [dict(r) for r in cur.fetchall()]


def get_change_history(conn: sqlite3.Connection, org_number: str) -> List[Dict[str, Any]]:
    """Retrieve change history records for a company."""
    valid_org = BrregClient.validate_org_number(org_number)
    cur = conn.execute("SELECT * FROM change_history WHERE org_number = ? ORDER BY change_id ASC;", (valid_org,))
    return [dict(r) for r in cur.fetchall()]


def get_company_profile_with_evidence(conn: sqlite3.Connection, org_number: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve full aggregated profile, active facts with attached evidence, and change history.
    """
    valid_org = BrregClient.validate_org_number(org_number)
    comp = get_company(conn, valid_org)
    if not comp:
        return None

    facts = get_facts(conn, valid_org, active_only=True)
    for fact in facts:
        fact["evidence"] = get_evidence_for_fact(conn, fact["fact_id"])

    history = get_change_history(conn, valid_org)

    return {
        "company": comp,
        "facts": facts,
        "change_history": history,
    }
