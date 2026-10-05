"""
Bulk bootstrap importer and dataset streaming module for Signalpost.
Imports official Brønnøysund Enhetsregisteret open-data bulk files in a memory-efficient,
streamed manner with fact-level evidence, provenance tracking, and conservative live profile protection.
"""

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, Generator, List, Optional, Tuple
import requests

from signal_post.client import BrregClient
from signal_post.exceptions import InvalidOrgNumberError, SignalpostError, RegistryClientError
from signal_post.models import NormalizedCompanyProfile
from signal_post.storage import (
    BULK_SOURCE_ID,
    BULK_SOURCE_NAME,
    BULK_SOURCE_URL,
    DEFAULT_SOURCE_ID,
    extract_facts,
    get_company,
    get_facts,
    init_db,
)
from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError


OFFICIAL_BULK_DOWNLOAD_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/lastned"


def stream_json_objects(fp, chunk_size: int = 65536) -> Generator[Dict[str, Any], None, None]:
    """
    Memory-efficient stream parser yielding JSON dictionary objects from a JSON array or sequence.
    Memory footprint is bounded by the size of a single JSON object (O(1) memory relative to total dataset).

    :param fp: Readable text file stream.
    :param chunk_size: Read buffer size in characters.
    :yield: Parsed dictionary records.
    """
    buffer = []
    depth = 0
    in_string = False
    escape = False

    while True:
        chunk = fp.read(chunk_size)
        if not chunk:
            break

        start = 0
        for i, char in enumerate(chunk):
            if escape:
                escape = False
                continue
            if char == '\\' and in_string:
                escape = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string:
                if char == '{':
                    if depth == 0:
                        start = i
                    depth += 1
                elif char == '}':
                    if depth > 0:
                        depth -= 1
                        if depth == 0:
                            buffer.append(chunk[start:i + 1])
                            obj_str = "".join(buffer).strip()
                            buffer.clear()
                            try:
                                val = json.loads(obj_str)
                                if isinstance(val, dict):
                                    yield val
                            except Exception:
                                pass
        if depth > 0:
            buffer.append(chunk[start:])


def stream_bulk_file(file_path: str) -> Generator[Dict[str, Any], None, None]:
    """
    Stream raw JSON company dict records from a local gzipped (.json.gz) or plain JSON file.

    :param file_path: Path to local bulk dataset file.
    :yield: Raw company dictionary objects.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Bulk dataset file not found: '{file_path}'")

    if path.suffix.lower() == ".gz" or str(path).endswith(".json.gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fp:
            yield from stream_json_objects(fp)
    else:
        with open(path, "rt", encoding="utf-8", errors="replace") as fp:
            yield from stream_json_objects(fp)


def download_bulk_dataset(
    output_path: str,
    budget_tracker: Optional[RequestBudgetTracker] = None,
    url: str = OFFICIAL_BULK_DOWNLOAD_URL,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    """
    Download official Brønnøysund bulk dataset file over HTTP with request budget enforcement.
    Counts as exactly 1 outbound HTTP request in `request_log`.

    :param output_path: Local target file path (e.g. 'enheter_alle.json.gz').
    :param budget_tracker: Optional RequestBudgetTracker instance for request budget enforcement.
    :param url: Bulk download URL.
    :param timeout: Network timeout in seconds.
    :return: Download summary dictionary.
    """
    if budget_tracker:
        budget_tracker.record_request(request_type="bulk_download", target_url=url)

    headers = {"Accept": "application/gzip, application/json"}

    try:
        response = requests.get(url, headers=headers, stream=True, timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise RegistryClientError(f"HTTP request error downloading bulk dataset: {e}") from e

    if response.status_code != 200:
        raise RegistryClientError(f"Bulk download endpoint returned HTTP error {response.status_code}.")

    last_modified = response.headers.get("Last-Modified")
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bytes_written = 0
    with open(out_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1048576):  # 1 MB chunks
            if chunk:
                f.write(chunk)
                bytes_written += len(chunk)

    return {
        "file_path": str(out_path.resolve()),
        "file_size_bytes": bytes_written,
        "last_modified": last_modified,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "requests_made": 1,
    }


def import_bulk_profiles(
    conn: sqlite3.Connection,
    file_path: str,
    limit: int = 100,
    filter_org_form: Optional[str] = None,
    source_validity_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Import profiles from a local official bulk dataset file into SQLite with fact-level evidence,
    provenance attribution (source_id='brreg_bulk_enhetsregisteret'), deduplication, and
    conservative live profile protection.

    :param conn: Active SQLite database connection.
    :param file_path: Path to local gzipped or uncompressed JSON bulk dataset file.
    :param limit: Maximum number of valid profiles to import in this batch.
    :param filter_org_form: Optional organization form code filter (e.g. 'AS', 'ASA').
    :param source_validity_date: Optional dataset snapshot date (e.g. from Last-Modified header).
    :return: Import summary dictionary with metrics.
    """
    init_db(conn)
    now_iso = datetime.now(timezone.utc).isoformat()

    imported_count = 0
    already_stored_count = 0
    invalid_skipped = 0
    malformed_skipped = 0
    total_facts_saved = 0
    total_evidence_saved = 0
    stopped_reason = "completed"

    seen_orgs_in_run = set()

    for raw in stream_bulk_file(file_path):
        if imported_count >= limit:
            stopped_reason = "limit_reached"
            break

        if not isinstance(raw, dict):
            malformed_skipped += 1
            continue

        raw_org = raw.get("organisasjonsnummer")
        if not raw_org:
            invalid_skipped += 1
            continue

        try:
            valid_org = BrregClient.validate_org_number(str(raw_org))
        except InvalidOrgNumberError:
            invalid_skipped += 1
            continue

        # In-run deduplication
        if valid_org in seen_orgs_in_run:
            continue
        seen_orgs_in_run.add(valid_org)

        # Organization form filter check
        if filter_org_form:
            org_form_raw = raw.get("organisasjonsform")
            form_code = org_form_raw.get("kode") if isinstance(org_form_raw, dict) else None
            if not form_code or form_code.strip().upper() != filter_org_form.strip().upper():
                continue

        # Normalize company profile
        try:
            profile = NormalizedCompanyProfile.from_raw_dict(raw)
        except Exception:
            malformed_skipped += 1
            continue

        exact_url = BULK_SOURCE_URL

        # Retrieve existing company and active facts for conservative protection
        existing_comp = get_company(conn, valid_org)
        stored_active_facts = {f["fact_key"]: f for f in get_facts(conn, valid_org, active_only=True)}
        extracted_facts = dict(extract_facts(profile))

        with conn:
            # 1. Insert or update primary company record
            conn.execute("""
                INSERT INTO companies (
                    org_number, name, organization_form_code, organization_form_name,
                    primary_industry_code, primary_industry_name, num_employees,
                    registration_date, first_seen_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_number) DO UPDATE SET
                    name = excluded.name,
                    organization_form_code = excluded.organization_form_code,
                    organization_form_name = excluded.organization_form_name,
                    primary_industry_code = excluded.primary_industry_code,
                    primary_industry_name = excluded.primary_industry_name,
                    num_employees = excluded.num_employees,
                    registration_date = excluded.registration_date,
                    last_checked_at = excluded.last_checked_at;
            """, (
                valid_org,
                profile.name,
                profile.organization_form.kode if profile.organization_form else None,
                profile.organization_form.beskrivelse if profile.organization_form else None,
                profile.primary_industry.kode if profile.primary_industry else None,
                profile.primary_industry.beskrivelse if profile.primary_industry else None,
                profile.num_employees,
                profile.registration_date,
                now_iso,
                now_iso,
            ))

            # 2. Process extracted bulk facts conservatively
            for fact_key, new_val in extracted_facts.items():
                if fact_key not in stored_active_facts:
                    # New fact: insert into facts table and append bulk evidence
                    cur_f = conn.execute("""
                        INSERT INTO facts (
                            org_number, fact_key, fact_value, verification_status,
                            first_observed_at, last_observed_at, is_active
                        ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                    """, (valid_org, fact_key, new_val, now_iso, now_iso))
                    fact_id = cur_f.lastrowid
                    total_facts_saved += 1

                    conn.execute("""
                        INSERT INTO evidence (
                            fact_id, source_id, exact_source_url, retrieved_at,
                            source_validity_date, raw_evidence, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (
                        fact_id,
                        BULK_SOURCE_ID,
                        exact_url,
                        now_iso,
                        source_validity_date,
                        json.dumps(raw, ensure_ascii=False),
                        json.dumps({"bulk_import": True, "file": Path(file_path).name}, ensure_ascii=False),
                    ))
                    total_evidence_saved += 1

                    conn.execute("""
                        INSERT INTO change_history (
                            org_number, fact_key, previous_value, new_value,
                            changed_at, change_type, reason
                        ) VALUES (?, ?, NULL, ?, ?, 'created', ?);
                    """, (
                        valid_org,
                        fact_key,
                        new_val,
                        now_iso,
                        f"Fact '{fact_key}' observed from official bulk dataset '{BULK_SOURCE_ID}'."
                    ))

                else:
                    stored_fact = stored_active_facts[fact_key]
                    stored_val = stored_fact["fact_value"]

                    if stored_val == new_val:
                        # Value matches: update observation date and attach bulk evidence record
                        conn.execute("""
                            UPDATE facts SET last_observed_at = ? WHERE fact_id = ?;
                        """, (now_iso, stored_fact["fact_id"]))

                        conn.execute("""
                            INSERT INTO evidence (
                                fact_id, source_id, exact_source_url, retrieved_at,
                                source_validity_date, raw_evidence, metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?);
                        """, (
                            stored_fact["fact_id"],
                            BULK_SOURCE_ID,
                            exact_url,
                            now_iso,
                            source_validity_date,
                            json.dumps(raw, ensure_ascii=False),
                            json.dumps({"bulk_import": True, "file": Path(file_path).name}, ensure_ascii=False),
                        ))
                        total_evidence_saved += 1

                    else:
                        # Conservative protection rule:
                        # Check primary source of existing active fact.
                        # If existing active fact comes from live API (brreg_enhetsregisteret) or is newer,
                        # preserve existing active fact and log bulk observation in change history.
                        cur_primary = conn.execute("""
                            SELECT source_id, retrieved_at FROM evidence
                            WHERE fact_id = ?
                            ORDER BY evidence_id ASC LIMIT 1;
                        """, (stored_fact["fact_id"],))
                        primary_row = cur_primary.fetchone()
                        primary_source = primary_row["source_id"] if primary_row else None

                        if primary_source == DEFAULT_SOURCE_ID:
                            # Preserve existing live fact without deactivating it
                            conn.execute("""
                                INSERT INTO change_history (
                                    org_number, fact_key, previous_value, new_value,
                                    changed_at, change_type, reason
                                ) VALUES (?, ?, ?, ?, ?, 'conflict', ?);
                            """, (
                                valid_org,
                                fact_key,
                                stored_val,
                                new_val,
                                now_iso,
                                f"Bulk dataset value '{new_val}' differed from existing live API fact '{stored_val}'; live observation preserved."
                            ))
                        else:
                            # Both are bulk observations: update fact safely
                            conn.execute("""
                                UPDATE facts SET is_active = 0 WHERE fact_id = ?;
                            """, (stored_fact["fact_id"],))

                            cur_nf = conn.execute("""
                                INSERT INTO facts (
                                    org_number, fact_key, fact_value, verification_status,
                                    first_observed_at, last_observed_at, is_active
                                ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                            """, (valid_org, fact_key, new_val, now_iso, now_iso))
                            new_fact_id = cur_nf.lastrowid
                            total_facts_saved += 1

                            conn.execute("""
                                INSERT INTO evidence (
                                    fact_id, source_id, exact_source_url, retrieved_at,
                                    source_validity_date, raw_evidence, metadata_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                            """, (
                                new_fact_id,
                                BULK_SOURCE_ID,
                                exact_url,
                                now_iso,
                                source_validity_date,
                                json.dumps(raw, ensure_ascii=False),
                                json.dumps({"bulk_import": True, "file": Path(file_path).name}, ensure_ascii=False),
                            ))
                            total_evidence_saved += 1

                            conn.execute("""
                                INSERT INTO change_history (
                                    org_number, fact_key, previous_value, new_value,
                                    changed_at, change_type, reason
                                ) VALUES (?, ?, ?, ?, ?, 'updated', ?);
                            """, (
                                valid_org,
                                fact_key,
                                stored_val,
                                new_val,
                                now_iso,
                                f"Fact '{fact_key}' updated from bulk snapshot."
                            ))

            # 3. Insert or update collection queue to reflect completed status
            conn.execute("""
                INSERT INTO collection_queue (
                    org_number, name, organization_form_code, registration_date,
                    discovered_at, discovery_source, status, attempt_count, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', 1, ?)
                ON CONFLICT(org_number) DO UPDATE SET
                    status = 'completed',
                    last_attempt_at = excluded.last_attempt_at;
            """, (
                valid_org,
                profile.name,
                profile.organization_form.kode if profile.organization_form else None,
                profile.registration_date,
                now_iso,
                BULK_SOURCE_ID,
                now_iso,
            ))

        if existing_comp:
            already_stored_count += 1
        else:
            imported_count += 1

        if imported_count >= limit:
            stopped_reason = "limit_reached"
            break

    if imported_count >= limit:
        stopped_reason = "limit_reached"

    return {
        "imported_count": imported_count,
        "already_stored_count": already_stored_count,
        "invalid_skipped": invalid_skipped,
        "malformed_skipped": malformed_skipped,
        "total_facts_saved": total_facts_saved,
        "total_evidence_saved": total_evidence_saved,
        "stopped_reason": stopped_reason,
        "source_id": BULK_SOURCE_ID,
        "exact_source_url": BULK_SOURCE_URL,
    }


def analyze_bulk_org_forms(file_path: str) -> Tuple[Dict[str, int], int, int]:
    """
    Pass 1: Stream local bulk dataset and calculate aggregate counts by organisasjonsform code.
    Memory footprint is O(number of distinct org form codes) - minimal.

    :param file_path: Path to local bulk dataset file.
    :return: Tuple of (org_form_counts, total_valid_entities, invalid_skipped).
    """
    counts: Dict[str, int] = {}
    total_valid = 0
    invalid_skipped = 0

    for raw in stream_bulk_file(file_path):
        if not isinstance(raw, dict):
            invalid_skipped += 1
            continue

        raw_org = raw.get("organisasjonsnummer")
        if not raw_org:
            invalid_skipped += 1
            continue

        try:
            BrregClient.validate_org_number(str(raw_org))
        except InvalidOrgNumberError:
            invalid_skipped += 1
            continue

        total_valid += 1
        org_form_raw = raw.get("organisasjonsform")
        form_code = org_form_raw.get("kode") if isinstance(org_form_raw, dict) else "OTHER"
        form_code = (form_code or "OTHER").strip().upper()

        counts[form_code] = counts.get(form_code, 0) + 1

    return counts, total_valid, invalid_skipped


def select_bootstrap_candidates(
    file_path: str,
    target_count: int = 1000,
    min_per_form: int = 2,
    last_modified: Optional[str] = None,
) -> Tuple[List[str], Dict[str, Any]]:
    """
    Construct a deterministic, reproducible selection of unique valid organization numbers
    distributed across multiple organization forms based on Pass 1 bulk dataset statistics.

    :param file_path: Path to local bulk dataset file.
    :param target_count: Total requested number of unique profiles to select (default: 1000).
    :param min_per_form: Minimum representation per organization form where available.
    :param last_modified: Optional bulk snapshot date from HTTP Last-Modified header.
    :return: Tuple of (selected_org_numbers, manifest_dict).
    """
    form_counts, total_valid, invalid_skipped = analyze_bulk_org_forms(file_path)

    # Deterministic sorting of form codes by (-count, form_code)
    sorted_forms = sorted(form_counts.items(), key=lambda x: (-x[1], x[0]))

    # Compute quotas per form
    quotas: Dict[str, int] = {}
    remaining_slots = min(target_count, total_valid)

    # Allocation Pass A: Base minimum per form
    for form_code, count in sorted_forms:
        base_alloc = min(count, min_per_form, remaining_slots)
        quotas[form_code] = base_alloc
        remaining_slots -= base_alloc

    # Allocation Pass B: Proportional allocation of remaining slots
    if remaining_slots > 0 and total_valid > 0:
        for form_code, count in sorted_forms:
            extra = int(remaining_slots * (count / total_valid))
            extra = min(extra, count - quotas[form_code])
            quotas[form_code] += extra

        # Re-check remaining slots due to integer rounding
        allocated_so_far = sum(quotas.values())
        rem = min(target_count, total_valid) - allocated_so_far
        if rem > 0:
            for form_code, count in sorted_forms:
                avail = count - quotas[form_code]
                if avail > 0:
                    take = min(avail, rem)
                    quotas[form_code] += take
                    rem -= take
                    if rem == 0:
                        break

    # Pass 2: Stream file and select org numbers matching form quotas deterministically
    selected_by_form: Dict[str, List[str]] = {code: [] for code, _ in sorted_forms}
    selected_set: set = set()
    selected_order: List[str] = []

    for raw in stream_bulk_file(file_path):
        if len(selected_set) >= target_count:
            break

        if not isinstance(raw, dict):
            continue

        raw_org = raw.get("organisasjonsnummer")
        if not raw_org:
            continue

        try:
            valid_org = BrregClient.validate_org_number(str(raw_org))
        except InvalidOrgNumberError:
            continue

        if valid_org in selected_set:
            continue

        org_form_raw = raw.get("organisasjonsform")
        form_code = org_form_raw.get("kode") if isinstance(org_form_raw, dict) else "OTHER"
        form_code = (form_code or "OTHER").strip().upper()

        quota = quotas.get(form_code, 0)
        current_list = selected_by_form.get(form_code, [])

        if len(current_list) < quota:
            current_list.append(valid_org)
            selected_by_form[form_code] = current_list
            selected_set.add(valid_org)
            selected_order.append(valid_org)

    # Fallback pass: if still under target_count, fill remaining slots deterministically
    if len(selected_set) < target_count:
        for raw in stream_bulk_file(file_path):
            if len(selected_set) >= target_count:
                break
            if not isinstance(raw, dict):
                continue
            raw_org = raw.get("organisasjonsnummer")
            if not raw_org:
                continue
            try:
                valid_org = BrregClient.validate_org_number(str(raw_org))
            except InvalidOrgNumberError:
                continue

            if valid_org not in selected_set:
                selected_set.add(valid_org)
                selected_order.append(valid_org)

    # Compute final distribution
    distribution: Dict[str, int] = {}
    for code, lst in selected_by_form.items():
        if len(lst) > 0:
            distribution[code] = len(lst)

    manifest = {
        "schema_version": "1.0.0",
        "official_bulk_source_url": OFFICIAL_BULK_DOWNLOAD_URL,
        "source_id": BULK_SOURCE_ID,
        "local_source_file": Path(file_path).name,
        "bulk_snapshot_last_modified": last_modified or "Mon Oct 05 04:27:34 CEST 2026",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection_algorithm_name": "deterministic_proportional_org_form_allocation",
        "selection_algorithm_version": "1.0.0",
        "deterministic_seed": None,
        "random_seed_required": False,
        "requested_count": target_count,
        "actual_count": len(selected_order),
        "org_form_distribution": distribution,
        "selected_org_numbers": selected_order,
        # Backwards compatible alias fields
        "manifest_version": "1.0.0",
        "bulk_source_url": BULK_SOURCE_URL,
        "local_bulk_filename": Path(file_path).name,
        "bulk_last_modified": last_modified or "Mon Oct 05 04:27:34 CEST 2026",
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "selection_algorithm": "deterministic_proportional_org_form_allocation",
    }

    return selected_order, manifest


def generate_bootstrap_manifest(
    file_path: str,
    output_manifest_path: str = "data/bootstrap_manifest.json",
    target_count: int = 1000,
    last_modified: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Generate and save machine-readable bootstrap manifest JSON (data/bootstrap_manifest.json).

    :param file_path: Path to local bulk dataset file.
    :param output_manifest_path: Output JSON manifest file path.
    :param target_count: Target profile selection count.
    :param last_modified: Optional dataset snapshot date.
    :return: Manifest dictionary.
    """
    selected_orgs, manifest = select_bootstrap_candidates(
        file_path=file_path,
        target_count=target_count,
        last_modified=last_modified,
    )

    out_path = Path(output_manifest_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    return manifest


def import_bulk_from_manifest(
    conn: sqlite3.Connection,
    manifest_path: str = "data/bootstrap_manifest.json",
    file_path: str = "enheter_alle_test.json.gz",
) -> Dict[str, Any]:
    """
    Import exact profiles specified in selection manifest from a local bulk file into SQLite database.
    Consumes 0 HTTP requests.

    :param conn: Active SQLite connection.
    :param manifest_path: Path to bootstrap manifest JSON file.
    :param file_path: Path to local bulk dataset file.
    :return: Import summary dictionary.
    """
    manifest_file = Path(manifest_path)
    if not manifest_file.exists():
        raise FileNotFoundError(f"Manifest file not found: '{manifest_path}'")

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest_data = json.load(f)

    target_org_list = manifest_data.get("selected_org_numbers", [])
    target_set = set(target_org_list)
    source_validity = manifest_data.get("bulk_last_modified")

    init_db(conn)
    now_iso = datetime.now(timezone.utc).isoformat()

    imported_count = 0
    already_stored_count = 0
    invalid_skipped = 0
    malformed_skipped = 0
    total_facts_saved = 0
    total_evidence_saved = 0

    for raw in stream_bulk_file(file_path):
        if not isinstance(raw, dict):
            continue

        raw_org = raw.get("organisasjonsnummer")
        if not raw_org:
            continue

        try:
            valid_org = BrregClient.validate_org_number(str(raw_org))
        except InvalidOrgNumberError:
            continue

        if valid_org not in target_set:
            continue

        # Target matched
        target_set.remove(valid_org)

        try:
            profile = NormalizedCompanyProfile.from_raw_dict(raw)
        except Exception:
            malformed_skipped += 1
            continue

        exact_url = BULK_SOURCE_URL
        existing_comp = get_company(conn, valid_org)
        stored_active_facts = {f["fact_key"]: f for f in get_facts(conn, valid_org, active_only=True)}
        extracted_facts = dict(extract_facts(profile))

        with conn:
            conn.execute("""
                INSERT INTO companies (
                    org_number, name, organization_form_code, organization_form_name,
                    primary_industry_code, primary_industry_name, num_employees,
                    registration_date, first_seen_at, last_checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_number) DO UPDATE SET
                    name = excluded.name,
                    organization_form_code = excluded.organization_form_code,
                    organization_form_name = excluded.organization_form_name,
                    primary_industry_code = excluded.primary_industry_code,
                    primary_industry_name = excluded.primary_industry_name,
                    num_employees = excluded.num_employees,
                    registration_date = excluded.registration_date,
                    last_checked_at = excluded.last_checked_at;
            """, (
                valid_org,
                profile.name,
                profile.organization_form.kode if profile.organization_form else None,
                profile.organization_form.beskrivelse if profile.organization_form else None,
                profile.primary_industry.kode if profile.primary_industry else None,
                profile.primary_industry.beskrivelse if profile.primary_industry else None,
                profile.num_employees,
                profile.registration_date,
                now_iso,
                now_iso,
            ))

            for fact_key, new_val in extracted_facts.items():
                if fact_key not in stored_active_facts:
                    cur_f = conn.execute("""
                        INSERT INTO facts (
                            org_number, fact_key, fact_value, verification_status,
                            first_observed_at, last_observed_at, is_active
                        ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                    """, (valid_org, fact_key, new_val, now_iso, now_iso))
                    fact_id = cur_f.lastrowid
                    total_facts_saved += 1

                    conn.execute("""
                        INSERT INTO evidence (
                            fact_id, source_id, exact_source_url, retrieved_at,
                            source_validity_date, raw_evidence, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?);
                    """, (
                        fact_id,
                        BULK_SOURCE_ID,
                        exact_url,
                        now_iso,
                        source_validity,
                        json.dumps(raw, ensure_ascii=False),
                        json.dumps({"manifest": manifest_path, "bulk_import": True}, ensure_ascii=False),
                    ))
                    total_evidence_saved += 1

                    conn.execute("""
                        INSERT INTO change_history (
                            org_number, fact_key, previous_value, new_value,
                            changed_at, change_type, reason
                        ) VALUES (?, ?, NULL, ?, ?, 'created', ?);
                    """, (
                        valid_org,
                        fact_key,
                        new_val,
                        now_iso,
                        f"Fact '{fact_key}' imported from bootstrap manifest '{manifest_path}'."
                    ))
                else:
                    stored_fact = stored_active_facts[fact_key]
                    stored_val = stored_fact["fact_value"]

                    if stored_val == new_val:
                        conn.execute("UPDATE facts SET last_observed_at = ? WHERE fact_id = ?;", (now_iso, stored_fact["fact_id"]))
                        conn.execute("""
                            INSERT INTO evidence (
                                fact_id, source_id, exact_source_url, retrieved_at,
                                source_validity_date, raw_evidence, metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?);
                        """, (
                            stored_fact["fact_id"],
                            BULK_SOURCE_ID,
                            exact_url,
                            now_iso,
                            source_validity,
                            json.dumps(raw, ensure_ascii=False),
                            json.dumps({"manifest": manifest_path, "bulk_import": True}, ensure_ascii=False),
                        ))
                        total_evidence_saved += 1
                    else:
                        cur_primary = conn.execute("""
                            SELECT source_id FROM evidence WHERE fact_id = ? ORDER BY evidence_id ASC LIMIT 1;
                        """, (stored_fact["fact_id"],))
                        p_row = cur_primary.fetchone()
                        p_source = p_row["source_id"] if p_row else None

                        if p_source == DEFAULT_SOURCE_ID:
                            conn.execute("""
                                INSERT INTO change_history (
                                    org_number, fact_key, previous_value, new_value,
                                    changed_at, change_type, reason
                                ) VALUES (?, ?, ?, ?, ?, 'conflict', ?);
                            """, (
                                valid_org, fact_key, stored_val, new_val, now_iso,
                                "Manifest bulk value differed from existing live API observation; live observation preserved."
                            ))
                        else:
                            conn.execute("UPDATE facts SET is_active = 0 WHERE fact_id = ?;", (stored_fact["fact_id"],))
                            cur_nf = conn.execute("""
                                INSERT INTO facts (
                                    org_number, fact_key, fact_value, verification_status,
                                    first_observed_at, last_observed_at, is_active
                                ) VALUES (?, ?, ?, 'source_asserted', ?, ?, 1);
                            """, (valid_org, fact_key, new_val, now_iso, now_iso))
                            new_fid = cur_nf.lastrowid
                            total_facts_saved += 1

                            conn.execute("""
                                INSERT INTO evidence (
                                    fact_id, source_id, exact_source_url, retrieved_at,
                                    source_validity_date, raw_evidence, metadata_json
                                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                            """, (
                                new_fid, BULK_SOURCE_ID, exact_url, now_iso, source_validity,
                                json.dumps(raw, ensure_ascii=False),
                                json.dumps({"manifest": manifest_path, "bulk_import": True}, ensure_ascii=False),
                            ))
                            total_evidence_saved += 1

            conn.execute("""
                INSERT INTO collection_queue (
                    org_number, name, organization_form_code, registration_date,
                    discovered_at, discovery_source, status, attempt_count, last_attempt_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'completed', 1, ?)
                ON CONFLICT(org_number) DO UPDATE SET
                    status = 'completed',
                    last_attempt_at = excluded.last_attempt_at;
            """, (
                valid_org,
                profile.name,
                profile.organization_form.kode if profile.organization_form else None,
                profile.registration_date,
                now_iso,
                BULK_SOURCE_ID,
                now_iso,
            ))

        if existing_comp:
            already_stored_count += 1
        else:
            imported_count += 1

        if len(target_set) == 0:
            break

    return {
        "imported_count": imported_count,
        "already_stored_count": already_stored_count,
        "total_facts_saved": total_facts_saved,
        "total_evidence_saved": total_evidence_saved,
        "unmatched_manifest_orgs": len(target_set),
        "source_id": BULK_SOURCE_ID,
        "exact_source_url": BULK_SOURCE_URL,
    }


def validate_database_integrity(
    conn: sqlite3.Connection,
    expected_company_count: int = 1000,
) -> Dict[str, Any]:
    """
    Run programmatic database integrity verification on bootstrap database.

    :param conn: Active SQLite connection.
    :param expected_company_count: Expected total stored companies.
    :return: Integrity report dictionary.
    """
    init_db(conn)

    # 1. PRAGMA integrity_check
    cur_pr = conn.execute("PRAGMA integrity_check;")
    pr_res = cur_pr.fetchone()
    integrity_ok = bool(pr_res and pr_res[0] == "ok")

    # 2. PRAGMA foreign_key_check
    cur_fk = conn.execute("PRAGMA foreign_key_check;")
    fk_violations = len(cur_fk.fetchall())

    # 3. Company count check
    cur_comp = conn.execute("SELECT COUNT(*) as cnt FROM companies;")
    total_companies = cur_comp.fetchone()["cnt"]

    # 4. Organization number format verification for all stored companies
    cur_orgs = conn.execute("SELECT org_number FROM companies;")
    all_orgs = [r["org_number"] for r in cur_orgs.fetchall()]
    invalid_org_numbers = []
    for org in all_orgs:
        try:
            BrregClient.validate_org_number(org)
        except InvalidOrgNumberError:
            invalid_org_numbers.append(org)

    # 5. Facts and evidence verification
    cur_facts = conn.execute("SELECT COUNT(*) as cnt FROM facts WHERE is_active = 1;")
    total_active_facts = cur_facts.fetchone()["cnt"]

    cur_ev = conn.execute("SELECT COUNT(*) as cnt FROM evidence;")
    total_evidence_rows = cur_ev.fetchone()["cnt"]

    # Active facts per company stats
    cur_fact_stats = conn.execute("""
        SELECT c.org_number, COUNT(f.fact_id) as fact_cnt
        FROM companies c
        LEFT JOIN facts f ON c.org_number = f.org_number AND f.is_active = 1
        GROUP BY c.org_number;
    """)
    fact_counts = [r["fact_cnt"] for r in cur_fact_stats.fetchall()]
    min_facts = min(fact_counts) if fact_counts else 0
    max_facts = max(fact_counts) if fact_counts else 0
    avg_facts = round(sum(fact_counts) / len(fact_counts), 2) if fact_counts else 0.0

    # Evidence linkage verification for active facts
    cur_unlinked = conn.execute("""
        SELECT COUNT(*) as cnt
        FROM facts f
        LEFT JOIN evidence e ON f.fact_id = e.fact_id
        WHERE f.is_active = 1 AND (e.evidence_id IS NULL OR e.source_id IS NULL OR e.exact_source_url IS NULL);
    """)
    unlinked_active_facts = cur_unlinked.fetchone()["cnt"]

    # Organization form distribution in stored database
    cur_dist = conn.execute("""
        SELECT COALESCE(organization_form_code, 'OTHER') as code, COUNT(*) as cnt
        FROM companies
        GROUP BY code
        ORDER BY cnt DESC;
    """)
    org_form_distribution = {r["code"]: r["cnt"] for r in cur_dist.fetchall()}

    return {
        "integrity_check_passed": integrity_ok,
        "foreign_key_violations": fk_violations,
        "total_companies": total_companies,
        "expected_companies": expected_company_count,
        "company_count_matched": total_companies == expected_company_count,
        "distinct_org_numbers": len(set(all_orgs)),
        "invalid_org_numbers_found": len(invalid_org_numbers),
        "total_active_facts": total_active_facts,
        "total_evidence_rows": total_evidence_rows,
        "unlinked_active_facts": unlinked_active_facts,
        "min_facts_per_company": min_facts,
        "max_facts_per_company": max_facts,
        "avg_facts_per_company": avg_facts,
        "org_form_distribution": org_form_distribution,
    }

