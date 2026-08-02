"""GitHub REST client.

GitHub is the whole platform here: the event bus (Actions), the secret store,
the database (issues + labels + an orphan branch), and the dashboard host
(Pages). No other infrastructure exists, by design.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from .transport import HttpError, Transport, transport_from_env

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

LABEL_AUTO = "devin:auto"
LABEL_PREFIX_CLASS = "class:"
LABEL_PREFIX_STATE = "swarm:"
PROVENANCE_MARKER = "<!-- swarm-finding:"


class GitHubClient:
    def __init__(
        self,
        repo: str | None = None,
        token: str | None = None,
        transport: Transport | None = None,
    ) -> None:
        self.repo = repo or os.environ.get("SWARM_REPO", "")
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self.http = transport or transport_from_env()
        if self.http.mode != "replay" and not (self.repo and self.token):
            raise RuntimeError("SWARM_REPO and GITHUB_TOKEN are required in live mode")

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _req(self, method: str, path: str, body: Any = None, allow_status: tuple = (), **params: Any) -> Any:
        url = path if path.startswith("http") else f"{API}{path}"
        return self.http.request(
            method, url, headers=self._headers, json_body=body, params=params, allow_status=allow_status
        ).json()

    def _paginate(self, path: str, **params: Any) -> list[Any]:
        out: list[Any] = []
        page = 1
        while True:
            batch = self._req("GET", path, per_page=100, page=page, **params)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    # -- issues ---------------------------------------------------------------
    def list_issues(self, labels: list[str] | None = None, state: str = "open") -> list[dict[str, Any]]:
        params: dict[str, Any] = {"state": state}
        if labels:
            params["labels"] = ",".join(labels)
        issues = self._paginate(f"/repos/{self.repo}/issues", **params)
        return [i for i in issues if "pull_request" not in i]

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}/issues/{number}")

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict[str, Any]:
        return self._req("POST", f"/repos/{self.repo}/issues", {"title": title, "body": body, "labels": labels})

    def comment(self, number: int, body: str) -> dict[str, Any]:
        return self._req("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body})

    def list_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/issues/{number}/comments")

    def set_labels(self, number: int, labels: list[str]) -> Any:
        return self._req("PUT", f"/repos/{self.repo}/issues/{number}/labels", {"labels": labels})

    def add_labels(self, number: int, labels: list[str]) -> Any:
        return self._req("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": labels})

    def remove_label(self, number: int, label: str) -> Any:
        return self._req("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{label}", allow_status=(404,))

    def close_issue(self, number: int, reason: str = "completed") -> Any:
        return self._req("PATCH", f"/repos/{self.repo}/issues/{number}", {"state": "closed", "state_reason": reason})

    def ensure_label(self, name: str, color: str = "ededed", description: str = "") -> None:
        try:
            self._req("POST", f"/repos/{self.repo}/labels", {"name": name, "color": color, "description": description[:100]})
        except HttpError as exc:
            if exc.status != 422:  # already exists
                raise

    # -- pull requests --------------------------------------------------------
    def get_pr(self, number: int) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}/pulls/{number}")

    def list_pr_reviews(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/reviews")

    def list_pr_review_comments(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/comments")

    def list_pr_files(self, number: int) -> list[dict[str, Any]]:
        return self._paginate(f"/repos/{self.repo}/pulls/{number}/files")

    def combined_status(self, sha: str) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}/commits/{sha}/status")

    def set_status(self, sha: str, state: str, context: str, description: str, target_url: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"state": state, "context": context, "description": description[:139]}
        if target_url:
            body["target_url"] = target_url
        return self._req("POST", f"/repos/{self.repo}/statuses/{sha}", body)

    def check_runs(self, sha: str) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs", allow_status=(404,))

    def merge_pr(self, number: int, method: str = "squash", title: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"merge_method": method}
        if title:
            body["commit_title"] = title
        return self._req("PUT", f"/repos/{self.repo}/pulls/{number}/merge", body, allow_status=(405, 409))

    def search_prs_for_issue(self, issue_number: int) -> list[dict[str, Any]]:
        """Find PRs whose branch or body references the issue (dedupe-safe)."""
        q = f"repo:{self.repo} is:pr in:body \"#{issue_number}\""
        res = self._req("GET", "/search/issues", q=q.replace(" ", "+"), per_page=20, allow_status=(422,))
        return (res or {}).get("items", []) if isinstance(res, dict) else []

    # -- contents / orphan branches ------------------------------------------
    def get_file(self, path: str, ref: str) -> dict[str, Any] | None:
        res = self._req("GET", f"/repos/{self.repo}/contents/{path}", ref=ref, allow_status=(404,))
        if isinstance(res, dict) and res.get("content") is not None:
            return res
        return None

    def read_json(self, path: str, ref: str) -> Any | None:
        f = self.get_file(path, ref)
        if not f:
            return None
        return json.loads(base64.b64decode(f["content"]).decode())

    def put_file(self, path: str, ref: str, content: str, message: str, sha: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": ref,
        }
        if sha:
            body["sha"] = sha
        return self._req("PUT", f"/repos/{self.repo}/contents/{path}", body, allow_status=(409, 422))

    def branch_exists(self, ref: str) -> bool:
        res = self._req("GET", f"/repos/{self.repo}/git/ref/heads/{ref}", allow_status=(404,))
        return bool(res and res.get("ref"))

    def create_orphan_branch(self, ref: str, readme: str) -> None:
        """Create `ref` as an orphan branch containing a single README commit."""
        if self.branch_exists(ref):
            return
        blob = self._req("POST", f"/repos/{self.repo}/git/blobs", {"content": readme, "encoding": "utf-8"})
        tree = self._req(
            "POST",
            f"/repos/{self.repo}/git/trees",
            {"tree": [{"path": "README.md", "mode": "100644", "type": "blob", "sha": blob["sha"]}]},
        )
        commit = self._req(
            "POST",
            f"/repos/{self.repo}/git/commits",
            {"message": f"chore(swarm): initialise {ref}", "tree": tree["sha"], "parents": []},
        )
        self._req("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{ref}", "sha": commit["sha"]})

    # -- repo admin -----------------------------------------------------------
    def repo_info(self) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}")

    def enable_issues(self) -> Any:
        return self._req("PATCH", f"/repos/{self.repo}", {"has_issues": True})

    def list_workflows(self) -> list[dict[str, Any]]:
        res = self._req("GET", f"/repos/{self.repo}/actions/workflows", per_page=100)
        return (res or {}).get("workflows", [])

    def disable_workflow(self, workflow_id: int) -> Any:
        return self._req("PUT", f"/repos/{self.repo}/actions/workflows/{workflow_id}/disable", allow_status=(403, 404))

    def enable_pages(self, branch: str = "gh-pages", path: str = "/") -> Any:
        return self._req(
            "POST",
            f"/repos/{self.repo}/pages",
            {"source": {"branch": branch, "path": path}},
            allow_status=(409, 403, 422),
        )
