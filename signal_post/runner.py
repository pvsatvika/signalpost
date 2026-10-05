"""
Evaluator-facing runner module for Signalpost.
Provides one-command batch execution, stable output schemas, run-level metrics,
per-company error isolation, and request-budget protection.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Union

from signal_post.budget import RequestBudgetExceededError, RequestBudgetTracker
from signal_post.client import BrregClient
from signal_post.exceptions import InvalidOrgNumberError, SignalpostError
from signal_post.refresh import refresh_company
from signal_post.storage import (
    get_company,
    get_evidence_for_fact,
    get_facts,
    init_db,
)


class CompanyRunner:
    """
    Evaluator-facing batch execution runner for Norwegian company research.
    Processes one or many organization numbers safely with failure isolation,
    evidence lineage formatting, and budget protection.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Optional[BrregClient] = None,
        budget_tracker: Optional[RequestBudgetTracker] = None,
        max_requests_limit: int = 2000,
    ):
        self.conn = conn
        init_db(self.conn)
        self.client = client or BrregClient()
        self.budget_tracker = budget_tracker or RequestBudgetTracker(self.conn, budget_limit=max_requests_limit)

    def process_company(
        self,
        raw_org_number: str,
        live_refresh: bool = True,
    ) -> Dict[str, Any]:
        """
        Process a single organization number through validation, database lookup,
        live refresh (if configured), fact extraction, and evidence formatting.

        :param raw_org_number: Input organization number string.
        :param live_refresh: If True, attempts live Brønnøysund API lookup/refresh.
        :return: Evaluator-ready company result dictionary.
        """
        result: Dict[str, Any] = {
            "org_number": str(raw_org_number).strip(),
            "name": None,
            "status": "failed",
            "facts": [],
            "source_urls": [],
            "retrieval_dates": [],
            "changes": [],
            "warnings": [],
            "errors": [],
            "summary": "",
        }

        # 1. Input Validation
        try:
            valid_org = BrregClient.validate_org_number(raw_org_number)
            result["org_number"] = valid_org
        except InvalidOrgNumberError as e:
            result["errors"].append(f"Invalid 9-digit Norwegian organization number format: '{raw_org_number}'")
            result["summary"] = f"Failed: Invalid organization number format '{raw_org_number}'."
            return result

        # 2. Check existing database profile
        existing_comp = get_company(self.conn, valid_org)

        # 3. Live refresh attempt if configured
        refreshed = False
        refresh_changes = []

        if live_refresh:
            try:
                # Enforce request budget before network transmission
                if not self.budget_tracker.can_make_request():
                    if existing_comp:
                        result["warnings"].append("Request budget exhausted. Profile served from local database cache.")
                    else:
                        result["errors"].append("Outbound HTTP request budget exhausted (hackathon limit: 2000).")
                        result["summary"] = f"Failed: Request budget exhausted for {valid_org}."
                        return result
                else:
                    refresh_result = refresh_company(
                        self.conn,
                        valid_org,
                        client=self.client,
                        budget_tracker=self.budget_tracker,
                    )
                    if refresh_result.status == "success":
                        refreshed = True
                        result["name"] = refresh_result.company_name
                        for change in refresh_result.changes:
                            if change.change_type != "unchanged":
                                refresh_changes.append({
                                    "fact_key": change.fact_key,
                                    "change_type": change.change_type,
                                    "previous_value": change.previous_value,
                                    "new_value": change.new_value,
                                    "explanation": change.explanation,
                                })
                    else:
                        err_msg = refresh_result.error_message or "Unknown refresh error"
                        if existing_comp:
                            result["warnings"].append(f"Live refresh failed ({err_msg}). Profile served from local database cache.")
                        else:
                            result["errors"].append(f"Live refresh failed: {err_msg}")
                            result["summary"] = f"Failed: {err_msg}"
                            return result
            except RequestBudgetExceededError:
                if existing_comp:
                    result["warnings"].append("Request budget exhausted. Profile served from local database cache.")
                else:
                    result["errors"].append("Outbound HTTP request budget exhausted (hackathon limit: 2000).")
                    result["summary"] = f"Failed: Request budget exhausted for {valid_org}."
                    return result
            except Exception as e:
                if existing_comp:
                    result["warnings"].append(f"Live refresh failed ({e}). Profile served from local database cache.")
                else:
                    result["errors"].append(f"Live refresh failed: {e}")
                    result["summary"] = f"Failed: {e}"
                    return result

        # 4. Fetch stored company profile and active facts from DB
        comp = get_company(self.conn, valid_org)
        if not comp:
            result["errors"].append(f"Company '{valid_org}' not found in local database or registry.")
            result["summary"] = f"Failed: Company {valid_org} not found."
            return result

        result["name"] = comp.get("name")
        if refreshed:
            result["status"] = "live_refreshed"
        elif existing_comp:
            if live_refresh:
                result["status"] = "local_fallback_after_refresh_failure"
            else:
                result["status"] = "served_from_local"
        else:
            result["status"] = "served_from_local"

        result["changes"] = refresh_changes

        # 5. Retrieve active facts & evidence lineage
        stored_facts = get_facts(self.conn, valid_org, active_only=True)
        formatted_facts: List[Dict[str, Any]] = []
        source_urls_set = set()
        retrieval_dates_set = set()

        source_name_map = {
            "brreg_enhetsregisteret": "Brønnøysund Register Centre - Enhetsregisteret",
            "brreg_bulk_enhetsregisteret": "Brønnøysund Bulk Open Data",
        }

        for f in stored_facts:
            evidences = get_evidence_for_fact(self.conn, f["fact_id"])
            primary_ev = evidences[0] if evidences else {}

            s_url = primary_ev.get("exact_source_url") or primary_ev.get("source_url")
            r_at = primary_ev.get("retrieved_at")
            s_id = primary_ev.get("source_id", "brreg_enhetsregisteret")
            s_name = source_name_map.get(s_id, primary_ev.get("source_name", "Brønnøysund Register Centre - Enhetsregisteret"))

            if s_url:
                source_urls_set.add(s_url)
            if r_at:
                retrieval_dates_set.add(r_at)

            formatted_facts.append({
                "fact_key": f["fact_key"],
                "value": f["fact_value"],
                "source_name": s_name,
                "source_url": s_url,
                "retrieved_at": r_at,
                "source_validity_date": primary_ev.get("source_validity_date"),
                "verification_status": f.get("verification_status", "source_asserted"),
            })

        result["facts"] = formatted_facts
        result["source_urls"] = sorted(list(source_urls_set))
        result["retrieval_dates"] = sorted(list(retrieval_dates_set))

        # 6. Concise Factual Summary (using non-misleading terminology)
        fact_count = len(formatted_facts)
        has_conflicts = any(f.get("verification_status") == "conflicting" for f in formatted_facts)
        has_corroborated = any(f.get("verification_status") == "corroborated" for f in formatted_facts)

        if has_conflicts:
            evidence_desc = "active facts (including conflicting assertions)"
        elif has_corroborated:
            evidence_desc = "active corroborated facts"
        else:
            evidence_desc = "active source-supported facts"

        if result["status"] == "live_refreshed":
            status_label = "Refreshed via official registry REST API."
        elif result["status"] == "served_from_local":
            status_label = "Loaded from local SQLite cache."
        elif result["status"] == "local_fallback_after_refresh_failure":
            status_label = "Served from local SQLite cache after refresh failure."
        else:
            status_label = "Loaded from database."

        result["summary"] = f"Company '{result['name']}' ({valid_org}): {fact_count} {evidence_desc}. {status_label}"

        return result

    def run_batch(
        self,
        org_numbers: List[str],
        live_refresh: bool = True,
        db_identifier: str = "signalpost.db",
        input_source_identifier: str = "input_org_numbers",
        output_mode: str = "batch_result",
    ) -> Dict[str, Any]:
        """
        Execute batch evaluation over a list of organization numbers.
        Isolated per-company processing ensures one failure does not break the batch.

        :param org_numbers: List of organization number strings.
        :param live_refresh: If True, attempts live API refresh.
        :param db_identifier: Path or identifier of SQLite database file.
        :param input_source_identifier: Path or identifier of input source.
        :param output_mode: Output destination identifier ('file', 'stdout', etc.).
        :return: Evaluation report dictionary with run_metrics and results list.
        """
        start_dt = datetime.now(timezone.utc)
        start_time = time.time()
        initial_requests = self.budget_tracker.get_today_request_count()

        results: List[Dict[str, Any]] = []
        success_count = 0
        failed_count = 0
        served_from_local_count = 0
        local_fallback_count = 0
        live_refreshed_count = 0

        for raw_org in org_numbers:
            try:
                comp_res = self.process_company(raw_org, live_refresh=live_refresh)
            except Exception as e:
                # Per-company fault isolation
                comp_res = {
                    "org_number": str(raw_org).strip(),
                    "name": None,
                    "status": "failed",
                    "facts": [],
                    "source_urls": [],
                    "retrieval_dates": [],
                    "changes": [],
                    "warnings": [],
                    "errors": [f"Unhandled processing error: {e}"],
                    "summary": f"Failed: Unhandled error {e}",
                }

            results.append(comp_res)

            status = comp_res.get("status")
            if status == "failed":
                failed_count += 1
            else:
                success_count += 1
                if status == "served_from_local":
                    served_from_local_count += 1
                elif status == "live_refreshed":
                    live_refreshed_count += 1
                elif status == "local_fallback_after_refresh_failure":
                    local_fallback_count += 1
                else:
                    served_from_local_count += 1

        elapsed = time.time() - start_time
        end_dt = datetime.now(timezone.utc)
        total_requests_made = self.budget_tracker.get_today_request_count() - initial_requests
        total_req_count = len(org_numbers)
        avg_time = (elapsed / total_req_count) if total_req_count > 0 else 0.0

        safe_db_id = Path(db_identifier).name if db_identifier else "signalpost.db"
        safe_input_id = Path(input_source_identifier).name if input_source_identifier else "input_org_numbers"

        run_metrics = {
            "schema_version": "1.0.0",
            "run_mode": "live" if live_refresh else "offline",
            "started_at": start_dt.isoformat(),
            "completed_at": end_dt.isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            "average_seconds_per_company": round(avg_time, 4),
            "requested_count": total_req_count,
            "processed_count": len(results),
            "success_count": success_count,
            "failed_count": failed_count,
            "served_from_local_count": served_from_local_count,
            "local_fallback_count": local_fallback_count,
            "live_refreshed_count": live_refreshed_count,
            "total_outbound_requests": total_requests_made,
            "request_budget_limit": self.budget_tracker.budget_limit if self.budget_tracker else 2000,
            "estimated_external_api_cost": "$0",
            "database_identifier": safe_db_id,
            "input_source": safe_input_id,
            "output_mode": output_mode,
        }

        return {
            "run_metrics": run_metrics,
            "results": results,
        }


def run_evaluation(
    org_numbers: List[str],
    db_path: str = "signalpost.db",
    live_refresh: bool = True,
    client: Optional[BrregClient] = None,
    budget_tracker: Optional[RequestBudgetTracker] = None,
    max_requests_limit: int = 2000,
    input_source_identifier: str = "input_org_numbers",
    output_mode: str = "file",
) -> Dict[str, Any]:
    """
    High-level evaluation entry point accepting organization numbers and producing
    evaluator-ready structured report JSON.
    """
    conn = sqlite3.connect(db_path)
    try:
        runner = CompanyRunner(
            conn=conn,
            client=client,
            budget_tracker=budget_tracker,
            max_requests_limit=max_requests_limit,
        )
        return runner.run_batch(
            org_numbers,
            live_refresh=live_refresh,
            db_identifier=db_path,
            input_source_identifier=input_source_identifier,
            output_mode=output_mode,
        )
    finally:
        conn.close()


def load_input_org_numbers(input_source: Union[str, Path]) -> List[str]:
    """
    Load organization numbers from a JSON file, raw text file, or stdin stream ('-').
    """
    content = ""
    if input_source == "-" or str(input_source) == "-":
        content = sys.stdin.read()
    else:
        p = Path(input_source)
        if not p.exists():
            raise FileNotFoundError(f"Input file not found: '{input_source}'")
        content = p.read_text(encoding="utf-8")

    content_clean = content.strip()
    if not content_clean:
        return []

    # Attempt JSON parse first
    try:
        data = json.loads(content_clean)
        if isinstance(data, list):
            return [str(x).strip() for x in data if x]
        if isinstance(data, dict):
            orgs = data.get("org_numbers") or data.get("organisasjonsnummer") or data.get("companies") or []
            if isinstance(orgs, list):
                return [str(x).strip() for x in orgs if x]
    except Exception:
        pass

    # Fallback line-delimited or comma-separated parsing
    lines = [line.strip().strip(",") for line in content_clean.splitlines() if line.strip()]
    return [line for line in lines if line]


import hashlib
import subprocess

def compute_file_sha256(file_path: Union[str, Path]) -> str:
    """Compute standard SHA-256 hex digest for a file."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found for SHA-256 calculation: '{file_path}'")
    sha = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def get_git_commit_hash() -> str:
    """Retrieve current Git commit hash if in a Git repository, otherwise return 'not_a_git_repository'."""
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return res.stdout.strip()
    except Exception:
        return "not_a_git_repository"


def generate_benchmark_metadata(
    input_file: Union[str, Path],
    output_file: Union[str, Path],
    db_file: Union[str, Path],
    command_executed: str,
    output_metadata_file: Union[str, Path] = "benchmark_metadata.json",
) -> Dict[str, Any]:
    """
    Generate immutable benchmark provenance metadata JSON recording SHA-256 hashes,
    measured metrics, SQLite integrity verification results, and environment metadata.
    """
    input_p = Path(input_file)
    output_p = Path(output_file)
    db_p = Path(db_file)

    if not output_p.exists():
        raise FileNotFoundError(f"Output result file not found: '{output_file}'")
    if not db_p.exists():
        raise FileNotFoundError(f"Database file not found: '{db_file}'")

    input_sha = compute_file_sha256(input_p)
    output_sha = compute_file_sha256(output_p)

    report = json.loads(output_p.read_text(encoding="utf-8"))
    metrics = report.get("run_metrics", {})
    results = report.get("results", [])

    conn = sqlite3.connect(str(db_p))
    try:
        cur_integ = conn.execute("PRAGMA integrity_check;")
        integ_res = cur_integ.fetchone()[0]
        cur_fk = conn.execute("PRAGMA foreign_key_check;")
        fk_violations = len(cur_fk.fetchall())
    finally:
        conn.close()

    metadata = {
        "schema_version": "1.0.0",
        "benchmark_name": "canonical_100_company_live_benchmark",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command_executed": command_executed,
        "input_file": input_p.name,
        "input_sha256": input_sha,
        "output_file": output_p.name,
        "output_sha256": output_sha,
        "database_file": db_p.name,
        "org_number_count": len(results),
        "db_integrity_status": integ_res,
        "foreign_key_violations": fk_violations,
        "measured_runtime_seconds": metrics.get("elapsed_seconds", 0.0),
        "requests_used": metrics.get("total_outbound_requests", 0),
        "success_count": metrics.get("success_count", 0),
        "failed_count": metrics.get("failed_count", 0),
        "live_refreshed_count": metrics.get("live_refreshed_count", 0),
        "served_from_local_count": metrics.get("served_from_local_count", 0),
        "local_fallback_count": metrics.get("local_fallback_count", 0),
        "estimated_external_api_cost": metrics.get("estimated_external_api_cost", "$0"),
        "git_commit_hash": get_git_commit_hash(),
    }

    meta_p = Path(output_metadata_file)
    meta_p.parent.mkdir(parents=True, exist_ok=True)
    meta_p.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    return metadata


def validate_canonical_benchmark(
    input_file: Union[str, Path],
    output_file: Union[str, Path],
    db_file: Union[str, Path],
) -> Dict[str, Any]:
    """
    Programmatically validate the canonical benchmark results, database integrity,
    request count reconciliation, and evidence linkage.
    """
    input_orgs = load_input_org_numbers(input_file)
    if len(input_orgs) != 100 or len(set(input_orgs)) != 100:
        raise ValueError(f"Input file must contain exactly 100 unique org numbers. Found: {len(input_orgs)} total, {len(set(input_orgs))} unique.")

    output_p = Path(output_file)
    report = json.loads(output_p.read_text(encoding="utf-8"))
    metrics = report.get("run_metrics", {})
    results = report.get("results", [])

    if len(results) != 100:
        raise ValueError(f"Output must contain exactly 100 result items. Found: {len(results)}.")

    # Metric Reconciliation
    req_c = metrics.get("requested_count")
    proc_c = metrics.get("processed_count")
    succ_c = metrics.get("success_count")
    fail_c = metrics.get("failed_count")
    live_c = metrics.get("live_refreshed_count")
    local_c = metrics.get("served_from_local_count")
    fall_c = metrics.get("local_fallback_count")

    if proc_c != 100 or req_c != 100:
        raise ValueError(f"Requested/Processed counts must be 100. Got: req={req_c}, proc={proc_c}")
    if proc_c != succ_c + fail_c:
        raise ValueError(f"Metric mismatch: processed_count ({proc_c}) != success ({succ_c}) + failed ({fail_c})")
    if succ_c != live_c + local_c + fall_c:
        raise ValueError(f"Metric mismatch: success_count ({succ_c}) != live ({live_c}) + local ({local_c}) + fallback ({fall_c})")

    # Result Item Validation
    total_facts = 0
    total_facts_with_evidence = 0

    for i, res_item in enumerate(results):
        expected_org = input_orgs[i]
        if res_item.get("org_number") != expected_org:
            raise ValueError(f"Result item {i} org_number mismatch: expected {expected_org}, got {res_item.get('org_number')}")

        status = res_item.get("status")
        if status not in ("live_refreshed", "served_from_local", "local_fallback_after_refresh_failure", "failed"):
            raise ValueError(f"Invalid status '{status}' for company {expected_org}")

        facts = res_item.get("facts", [])
        total_facts += len(facts)
        for f in facts:
            if f.get("source_name") and f.get("source_url") and f.get("retrieved_at"):
                total_facts_with_evidence += 1
            else:
                raise ValueError(f"Fact '{f.get('fact_key')}' for {expected_org} missing evidence lineage.")

        if "verified active facts" in res_item.get("summary", "").lower():
            raise ValueError(f"Summary contains misleading 'verified active facts' term for {expected_org}")

    # Database Integrity & FK check
    conn = sqlite3.connect(str(db_file))
    try:
        cur_integ = conn.execute("PRAGMA integrity_check;")
        integ_val = cur_integ.fetchone()[0]
        if integ_val != "ok":
            raise ValueError(f"SQLite PRAGMA integrity_check failed: {integ_val}")

        cur_fk = conn.execute("PRAGMA foreign_key_check;")
        fk_rows = cur_fk.fetchall()
        if len(fk_rows) > 0:
            raise ValueError(f"SQLite PRAGMA foreign_key_check failed: {len(fk_rows)} violations found.")
    finally:
        conn.close()

    return {
        "validation_passed": True,
        "input_org_count": len(input_orgs),
        "result_count": len(results),
        "total_facts_verified": total_facts,
        "evidence_linkage_rate": (total_facts_with_evidence / total_facts) if total_facts > 0 else 1.0,
        "db_integrity": integ_val,
        "foreign_key_violations": len(fk_rows),
    }

