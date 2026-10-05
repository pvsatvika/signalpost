"""
Company discovery module for searching Norwegian organizations from Brønnøysund Enhetsregisteret API.
"""

from datetime import datetime, timezone
import json
import sqlite3
from typing import Any, Dict, List, Optional
import requests

from signal_post.client import BrregClient
from signal_post.exceptions import RegistryClientError, InvalidOrgNumberError
from signal_post.budget import RequestBudgetTracker, RequestBudgetExceededError
from signal_post.storage import init_db, DEFAULT_SOURCE_ID

BRREG_SEARCH_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"


def discover_organizations(
    conn: sqlite3.Connection,
    client: Optional[BrregClient] = None,
    budget_tracker: Optional[RequestBudgetTracker] = None,
    filter_org_form: Optional[str] = "AS",
    search_name: Optional[str] = None,
    page_size: int = 100,
    limit: int = 100,
) -> Dict[str, Any]:
    """
    Search Brønnøysund Enhetsregisteret API for Norwegian organizations, validate 9-digit numbers,
    deduplicate, and store discovery candidates into the local SQLite collection queue.

    :param conn: SQLite database connection.
    :param client: Optional custom or mocked BrregClient (uses session if provided).
    :param budget_tracker: Optional RequestBudgetTracker for enforcing request limits.
    :param filter_org_form: Optional organization form filter (e.g., 'AS', 'ASA', 'ENK').
    :param search_name: Optional company name search filter.
    :param page_size: Results per API page (max 100).
    :param limit: Maximum number of organization numbers to discover in this run.
    :return: Summary dictionary with discovery metrics.
    """
    init_db(conn)
    now_iso = datetime.now(timezone.utc).isoformat()
    session = client.session if client else requests.Session()
    actual_page_size = min(max(1, page_size), 100)

    discovered_new = 0
    already_queued = 0
    invalid_skipped = 0
    pages_fetched = 0
    requests_made = 0
    stopped_reason = "completed"

    page = 0
    total_pages = 1

    while discovered_new < limit and page < total_pages:
        if budget_tracker and not budget_tracker.can_make_request():
            stopped_reason = "request_budget_exceeded"
            break

        # Build query parameters
        params = {
            "page": page,
            "size": actual_page_size,
        }
        if filter_org_form:
            params["organisasjonsform"] = filter_org_form.strip()
        if search_name:
            params["navn"] = search_name.strip()

        # Format URL for request tracking
        param_str = "&".join(f"{k}={v}" for k, v in params.items())
        request_url = f"{BRREG_SEARCH_URL}?{param_str}"

        if budget_tracker:
            try:
                budget_tracker.record_request(request_type="discovery", target_url=request_url)
            except RequestBudgetExceededError:
                stopped_reason = "request_budget_exceeded"
                break
        requests_made += 1

        try:
            resp = session.get(BRREG_SEARCH_URL, params=params, headers={"Accept": "application/json"}, timeout=10.0)
        except Exception as e:
            raise RegistryClientError(f"Network error during discovery API search on page {page}: {e}") from e

        if resp.status_code != 200:
            raise RegistryClientError(f"Discovery API returned HTTP error {resp.status_code} on page {page}.")

        try:
            data = resp.json()
        except Exception as e:
            raise RegistryClientError(f"Invalid JSON returned by discovery search API on page {page}: {e}") from e

        if not isinstance(data, dict):
            raise RegistryClientError(f"Malformed search response format on page {page}: expected JSON object.")

        pages_fetched += 1
        page_meta = data.get("page", {})
        if isinstance(page_meta, dict):
            raw_tp = page_meta.get("totalPages")
            if isinstance(raw_tp, int) and raw_tp > 0:
                total_pages = raw_tp

        embedded = data.get("_embedded", {})
        enheter = embedded.get("enheter", []) if isinstance(embedded, dict) else []

        if not enheter:
            break

        for item in enheter:
            if not isinstance(item, dict):
                continue

            raw_org = item.get("organisasjonsnummer")
            if not raw_org:
                continue

            try:
                valid_org = BrregClient.validate_org_number(str(raw_org))
            except InvalidOrgNumberError:
                invalid_skipped += 1
                continue

            name = item.get("navn", "").strip() or "Unknown Company"
            org_form_code = None
            org_form_raw = item.get("organisasjonsform")
            if isinstance(org_form_raw, dict):
                org_form_code = org_form_raw.get("kode")

            reg_date = item.get("registreringsdatoEnhetsregisteret")

            # Check if already in queue
            cur_check = conn.execute("SELECT status FROM collection_queue WHERE org_number = ?;", (valid_org,))
            existing = cur_check.fetchone()

            if existing:
                already_queued += 1
            else:
                with conn:
                    conn.execute("""
                        INSERT INTO collection_queue (
                            org_number, name, organization_form_code, registration_date,
                            discovered_at, discovery_source, status
                        ) VALUES (?, ?, ?, ?, ?, ?, 'pending');
                    """, (valid_org, name, org_form_code, reg_date, now_iso, DEFAULT_SOURCE_ID))
                discovered_new += 1

            if discovered_new >= limit:
                stopped_reason = "limit_reached"
                break

        page += 1

    # Query queue status totals
    cur_tot = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue;")
    total_queued = cur_tot.fetchone()["cnt"]

    cur_pend = conn.execute("SELECT COUNT(*) as cnt FROM collection_queue WHERE status = 'pending';")
    pending_queued = cur_pend.fetchone()["cnt"]

    return {
        "discovered_new": discovered_new,
        "already_queued": already_queued,
        "invalid_skipped": invalid_skipped,
        "total_in_queue": total_queued,
        "pending_in_queue": pending_queued,
        "pages_fetched": pages_fetched,
        "requests_made": requests_made,
        "stopped_reason": stopped_reason,
    }
