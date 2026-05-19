"""
connection.py
=============
WhatsApp Graph API connection factory.

Holds the session and base URL for one account.
Switch accounts by changing config.yaml only — no code changes needed.

Public API:
    get_connection(account)           -> GraphAPIConnection
    close_connection(conn)            -> None
    test_connection(account)          -> int  (1=ok, -1=error)
"""

import requests
import logging

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v19.0"


# ── Connection object ─────────────────────────────────────────

class GraphAPIConnection:
    """Thin wrapper around requests.Session for one WhatsApp account."""

    def __init__(self, phone_number_id: str, access_token: str):
        self.phone_number_id = phone_number_id
        self.access_token    = access_token
        self.base_url        = f"{GRAPH_BASE}/{phone_number_id}"
        self.session         = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type" : "application/json",
        })

    def get(self, url: str, **kwargs):
        return self.session.get(url, **kwargs)

    def post(self, endpoint: str, json: dict, **kwargs):
        return self.session.post(f"{self.base_url}/{endpoint}", json=json, **kwargs)

    def close(self):
        self.session.close()


# ── Public API ────────────────────────────────────────────────

def get_connection(account: dict) -> GraphAPIConnection:
    """Return an open GraphAPIConnection for the given account."""
    return GraphAPIConnection(
        phone_number_id=account["phone_number_id"],
        access_token=account["access_token"],
    )


def close_connection(conn: GraphAPIConnection) -> None:
    """Close the session."""
    try:
        conn.close()
    except Exception:
        pass


def test_connection(account: dict) -> int:
    """Smoke-test by hitting the phone number endpoint. Returns 1=ok, -1=error."""
    try:
        conn = get_connection(account)
        url  = f"{GRAPH_BASE}/{account['phone_number_id']}"
        resp = conn.get(url, params={"fields": "display_phone_number", "access_token": account["access_token"]})
        conn.close()
        if resp.status_code == 200:
            logger.info("connection OK: %s", account.get("name"))
            return 1
        logger.error("connection FAIL: %s - HTTP %s", account.get("name"), resp.status_code)
        return -1
    except Exception as exc:
        logger.error("connection FAIL: %s - %s", account.get("name"), exc)
        return -1