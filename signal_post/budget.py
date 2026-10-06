"""
Outbound HTTP request budget tracker for Signalpost.
Ensures daily outbound API request limits (e.g., hackathon 2,000 requests/day limit) are strictly respected.
"""

from datetime import datetime, timezone
import sqlite3
from typing import Optional


from signal_post.exceptions import SignalpostError


class RequestBudgetExceededError(SignalpostError):
    """Raised when the configured request budget limit is reached."""
    pass


class RequestBudgetTracker:
    """Tracks and enforces outbound HTTP request limits locally via SQLite."""

    def __init__(self, conn: sqlite3.Connection, budget_limit: int = 100):
        self.conn = conn
        self.budget_limit = budget_limit
        self._init_table()

    def _init_table(self) -> None:
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_type TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
            """)

    def count_requests_today(self) -> int:
        """Count outbound requests logged today (UTC day YYYY-MM-DD)."""
        today_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = self.conn.execute(
            "SELECT COUNT(*) as count FROM request_log WHERE timestamp LIKE ?;",
            (f"{today_prefix}%",)
        )
        row = cur.fetchone()
        return row[0] if row else 0

    def get_today_request_count(self) -> int:
        """Alias for count_requests_today()."""
        return self.count_requests_today()

    def get_max_request_id(self) -> int:
        """Highest request_log id so far (0 if empty). Used as a per-run delta anchor."""
        row = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM request_log;").fetchone()
        return row[0] if row else 0

    def count_requests_since_id(self, anchor_id: int) -> dict:
        """Requests logged after anchor_id, grouped by request_type (actual per-run delta)."""
        rows = self.conn.execute(
            "SELECT request_type, COUNT(*) FROM request_log WHERE id > ? GROUP BY request_type;",
            (anchor_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def get_remaining_budget(self) -> int:
        """Get remaining allowed requests for today."""
        used = self.count_requests_today()
        return max(0, self.budget_limit - used)

    def can_make_request(self) -> bool:
        """Check if at least 1 request budget remains."""
        return self.count_requests_today() < self.budget_limit

    def record_request(self, request_type: str, target_url: str) -> None:
        """
        Log an outbound request. Raises RequestBudgetExceededError if budget limit reached.
        """
        if not self.can_make_request():
            raise RequestBudgetExceededError(
                f"Outbound request budget limit reached ({self.budget_limit} requests today). Stopping safely."
            )

        now_iso = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO request_log (request_type, target_url, timestamp) VALUES (?, ?, ?);",
                (request_type, target_url, now_iso)
            )
