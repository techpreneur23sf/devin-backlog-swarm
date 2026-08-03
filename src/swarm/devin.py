"""Devin v3 API client.

Only the endpoints the swarm actually needs, verified against a real service
user on 2026-08-01 (see DECISIONS.md, D-001):

    GET  /v3/self                                    identity / smoke test
    POST /v3/organizations/{org}/sessions            dispatch
    GET  /v3/organizations/{org}/sessions            list (tag-based rebuild)
    GET  /v3/organizations/{org}/sessions/{id}       reconcile
    POST /v3/organizations/{org}/sessions/{id}/messages
    POST /v3/organizations/{org}/sessions/{id}/terminate
    GET  /v3/organizations/{org}/sessions/{id}/insights  effort (size, messages)
    GET  /v3/organizations/{org}/consumption/daily   ACU budget
    POST /v3/organizations/{org}/pr-reviews          Devin Review
    GET  /v3/organizations/{org}/pr-reviews?pr_url=  Devin Review verdict
"""

from __future__ import annotations

import os
from typing import Any

from .transport import Transport, transport_from_env

API_BASE = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai")

#: Required of every remediation session. `confidence` and `blockers` are the
#: inputs to the auto-merge decision in policy.py.
REMEDIATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["outcome", "summary", "confidence"],
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["fixed", "partial", "blocked", "not_reproducible", "wont_fix"],
        },
        "summary": {"type": "string", "maxLength": 600},
        "pr_url": {"type": ["string", "null"]},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "verification_commands_run": {"type": "array", "items": {"type": "string"}},
        "verification_passed": {"type": "boolean"},
        "tests_added": {"type": "integer"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "blockers": {"type": "array", "items": {"type": "string"}},
        "human_review_reason": {"type": ["string", "null"]},
    },
}


class DevinClient:
    def __init__(
        self,
        api_key: str | None = None,
        org_id: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("DEVIN_API_KEY", "")
        self.org_id = org_id or os.environ.get("DEVIN_ORG_ID", "")
        self.http = transport or transport_from_env()
        if self.http.mode != "replay" and not (self.api_key and self.org_id):
            raise RuntimeError("DEVIN_API_KEY and DEVIN_ORG_ID are required in live mode")

    # -- plumbing -------------------------------------------------------------
    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _url(self, path: str) -> str:
        return f"{API_BASE}/v3/organizations/{self.org_id}{path}"

    def _get(self, path: str, **params: Any) -> Any:
        return self.http.request("GET", self._url(path), headers=self._headers, params=params).json()

    def _post(self, path: str, body: Any, allow_status: tuple = ()) -> Any:
        return self.http.request(
            "POST", self._url(path), headers=self._headers, json_body=body, allow_status=allow_status
        ).json()

    # -- endpoints ------------------------------------------------------------
    def whoami(self) -> dict[str, Any]:
        return self.http.request("GET", f"{API_BASE}/v3/self", headers=self._headers).json()

    def create_session(
        self,
        prompt: str,
        title: str,
        tags: list[str],
        repos: list[str] | None = None,
        playbook_id: str | None = None,
        max_acu_limit: int | None = None,
        structured_output_schema: dict[str, Any] | None = None,
        knowledge_ids: list[str] | None = None,
        idempotent: bool = True,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "prompt": prompt,
            "title": title[:200],
            "tags": tags,
            "structured_output_schema": structured_output_schema or REMEDIATION_SCHEMA,
            "structured_output_required": True,
        }
        if repos:
            body["repos"] = repos
        if playbook_id:
            body["playbook_id"] = playbook_id
        if max_acu_limit:
            body["max_acu_limit"] = max_acu_limit
        if knowledge_ids:
            body["knowledge_ids"] = knowledge_ids
        if idempotent:
            body["idempotent"] = True
        return self._post("/sessions", body)

    def get_session(self, session_id: str) -> dict[str, Any]:
        return self._get(f"/sessions/{session_id}")

    def list_sessions(self, tags: list[str] | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """List sessions, optionally filtered by tag (used by `state rebuild`)."""
        items: list[dict[str, Any]] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            if tags:
                params["tags"] = ",".join(tags)
            page = self._get("/sessions", **params)
            items.extend(page.get("items", []))
            if not page.get("has_next_page"):
                break
            cursor = page.get("end_cursor")
            if not cursor:
                break
        if tags:
            wanted = set(tags)
            items = [s for s in items if wanted.issubset(set(s.get("tags") or []))]
        return items

    def send_message(self, session_id: str, message: str) -> Any:
        return self._post(f"/sessions/{session_id}/messages", {"message": message}, allow_status=(404,))

    def terminate(self, session_id: str) -> Any:
        return self._post(f"/sessions/{session_id}/terminate", {}, allow_status=(404, 409))

    def get_insights(self, session_id: str) -> dict[str, Any]:
        """Per-session effort: size class, message counts, category.

        The only usage signal the API reports on a plan that does not meter
        ACUs. 404s while a session is too short to analyse.
        """
        return self.http.request(
            "GET",
            self._url(f"/sessions/{session_id}/insights"),
            headers=self._headers,
            allow_status=(404, 409, 425),
        ).json() or {}

    def daily_consumption(self) -> dict[str, Any]:
        return self._get("/consumption/daily")

    # -- Devin Review ---------------------------------------------------------
    def request_pr_review(self, pr_url: str) -> dict[str, Any]:
        return self._post("/pr-reviews", {"pr_url": pr_url}, allow_status=(400, 403, 404, 409))

    def get_pr_review(self, pr_url: str) -> dict[str, Any]:
        return self.http.request(
            "GET",
            self._url("/pr-reviews"),
            headers=self._headers,
            params={"pr_url": pr_url},
            allow_status=(400, 403, 404),
        ).json()


def acus_today(client: DevinClient) -> float:
    """ACUs consumed in the current billing day, straight from the API.

    Reports 0.0 on accounts Devin does not meter in ACUs (self-serve plans bill
    included quota then on-demand credits, and neither is exposed per session).
    A budget that only counts observed ACUs is therefore not a budget at all on
    such a plan — see `reserved_acus_today`, which is what the scheduler spends.
    """
    data = client.daily_consumption() or {}
    by_date = data.get("consumption_by_date") or []
    if by_date:
        return float(by_date[-1].get("acus", by_date[-1].get("total_acus", 0.0)) or 0.0)
    return float(data.get("total_acus", 0.0) or 0.0)
