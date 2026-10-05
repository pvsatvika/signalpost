"""
Controlled, resumable collection workflow module for Signalpost.
"""

from datetime import datetime, timezone
import sqlite3
from typing import Any, Dict, List, Optional

from signal_post.client import BrregClient
from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError
from signal_post.refresh import refresh_company, RefreshResult
from signal_post.storage import init_db, get_company


def get_collection_status(conn: sqlite3.Connection, budget_tracker: Optional[RequestBudgetTracker] = None) -> Dict[str, Any]:
    """
    Query summary of discovery queue and collection status from SQLite database.
    """
    init_db(conn)

    cur_tot = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue;")
    total_queued = cur_tot.fetchone()["cnt"]

    cur_pend = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue WHERE status = 'pending';")
    pending_count = cur_pend.fetchone()["cnt"]

    cur_comp = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue WHERE status = 'completed';")
    completed_count = cur_comp.fetchone()["cnt"]

    cur_fail = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue WHERE status = 'failed';")
    failed_count = cur_fail.fetchone()["cnt"]

    cur_comp_tbl = conn.execute("SELECT COUNT(*) as cnt FROM companies;")
    stored_companies_count = cur_comp_tbl.fetchone()["cnt"]

    requests_today = budget_tracker.count_requests_today() if budget_tracker else 0
    remaining_budget = budget_tracker.get_remaining_budget() if budget_tracker else None

    return {
        "total_queued": total_queued,
        "pending_count": pending_count,
        "completed_count": completed_count,
        "failed_count": failed_count,
        "stored_companies_count": stored_companies_count,
        "requests_today": requests_today,
        "remaining_budget": remaining_budget,
    }


def collect_queued_profiles(
    conn: sqlite3.Connection,
    client: Optional[BrregClient] = None,
    budget_tracker: Optional[RequestBudgetTracker] = None,
    limit: int = 50,
    resume_failed: bool = False,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """
    Process pending or failed organization numbers in the local collection queue.
    Executes profile fetch, identity validation, normalization, and SQLite evidence storage safely.
    Supports resuming interrupted or failed collection runs and respects outbound request budgets.

    :param conn: SQLite database connection.
    :param client: Optional custom or mocked BrregClient.
    :param budget_tracker: Optional RequestBudgetTracker.
    :param limit: Maximum number of profiles to process in this collection run.
    :param resume_failed: Include failed org numbers in processing queue.
    :param force_refresh: Process completed org numbers as well.
    :return: Summary dictionary with collection metrics.
    """
    init_db(conn)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Query target items from collection queue
    if force_refresh:
        query = "SELECT org_number, attempt_count FROM collection_queue ORDER BY discovered_at ASC LIMIT ?;"
        params = (limit,)
    elif resume_failed:
        query = "SELECT org_number, attempt_count FROM collection_queue WHERE status IN ('pending', 'failed') ORDER BY attempt_count ASC, discovered_at ASC LIMIT ?;"
        params = (limit,)
    else:
        query = "SELECT org_number, attempt_count FROM collection_queue WHERE status = 'pending' ORDER BY discovered_at ASC LIMIT ?;"
        params = (limit,)

    cur_queue = conn.execute(query, params)
    target_rows = cur_queue.fetchall()

    attempted = 0
    completed = 0
    failed = 0
    requests_made = 0
    stopped_reason = "completed"

    for row in target_rows:
        org_number = row["org_number"]

        # Check request budget before attempting next collection
        if budget_tracker and not budget_tracker.can_make_request():
            stopped_reason = "request_budget_exceeded"
            break

        request_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}"
        if budget_tracker:
            try:
                budget_tracker.record_request(request_type="collection", target_url=request_url)
            except RequestBudgetExceededError:
                stopped_reason = "request_budget_exceeded"
                break

        op_time = datetime.now(timezone.utc).isoformat()
        attempted += 1
        requests_made += 1

        try:
            res = refresh_company(conn, org_number, client=client)

            if res.status == "success":
                with conn:
                    conn.execute("""
                        UPDATE collection_queue SET
                            status = 'completed',
                            attempt_count = attempt_count + 1,
                            last_attempt_at = ?,
                            error_message = NULL
                        WHERE org_number = ?;
                    """, (op_time, org_number))
                completed += 1
            else:
                with conn:
                    conn.execute("""
                        UPDATE collection_queue SET
                            status = 'failed',
                            attempt_count = attempt_count + 1,
                            last_attempt_at = ?,
                            error_message = ?
                        WHERE org_number = ?;
                    """, (op_time, res.error_message, org_number))
                failed += 1

                if budget_tracker and not budget_tracker.can_make_request():
                    stopped_reason = "request_budget_exceeded"
                    break

        except Exception as e:
            with conn:
                conn.execute("""
                    UPDATE collection_queue SET
                        status = 'failed',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        error_message = ?
                    WHERE org_number = ?;
                """, (op_time, f"Unhandled exception: {e}", org_number))
            failed += 1

    status_summary = get_collection_status(conn, budget_tracker=budget_tracker)

    return {
        "attempted": attempted,
        "completed": completed,
        "failed": failed,
        "requests_made": requests_made,
        "stopped_reason": stopped_reason,
        "total_queued": status_summary["total_queued"],
        "pending_in_queue": status_summary["pending_count"],
        "completed_in_queue": status_summary["completed_count"],
        "failed_in_queue": status_summary["failed_count"],
        "stored_companies_count": status_summary["stored_companies_count"],
        "requests_today": status_summary["requests_today"],
        "remaining_budget": status_summary["remaining_budget"],
    }
