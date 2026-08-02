"""HTTP transport with three modes: live, record, replay.

Every outbound call to the Devin API and the GitHub API goes through here, so
the offline replay mode (`swarm replay`) is a property of the transport rather
than of the business logic. The reconciler cannot tell the difference, which is
the point: the loop a reviewer watches offline is the same code path that ran
against the real APIs.

Fixtures are recordings of real runs. Nothing here invents a response: replay
of an unrecorded request is an error, not a fallback.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

LIVE = "live"
RECORD = "record"
REPLAY = "replay"

_REDACT_HEADERS = {"authorization", "x-api-key", "cookie", "set-cookie"}
_SECRET_RE = re.compile(r"(cog_|ghp_|ghs_|github_pat_)[A-Za-z0-9_\-]{6,}")


class ReplayMiss(RuntimeError):
    """Raised when replay mode is asked for a request that was never recorded."""


class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"{method} {url} -> {status}: {body[:500]}")
        self.method = method
        self.url = url
        self.status = status
        self.body = body


def _redact(text: str) -> str:
    return _SECRET_RE.sub(lambda m: m.group(1) + "REDACTED", text or "")


def _canonical(method: str, url: str, body: Any | None) -> str:
    payload = json.dumps(body, sort_keys=True, default=str) if body is not None else ""
    raw = f"{method.upper()} {url}\n{payload}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


@dataclass
class Response:
    status: int
    body: Any
    headers: dict[str, str]

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return self.body


class Transport:
    """Records and replays HTTP interactions keyed by (method, url, body)."""

    def __init__(
        self,
        mode: str = LIVE,
        cassette_dir: str | None = None,
        timeout: int = 30,
        session: requests.Session | None = None,
    ) -> None:
        self.mode = mode
        self.timeout = timeout
        self.cassette_dir = Path(cassette_dir) if cassette_dir else None
        self._session = session or requests.Session()
        self._cassette: dict[str, Any] = {}
        self._counts: dict[str, int] = {}
        self.calls: list = []
        #: replay requests the cassette did not contain — a fixture recorded
        #: against older code no longer describes what the code now asks for
        self.misses: list[str] = []
        if self.mode in (RECORD, REPLAY):
            if not self.cassette_dir:
                raise ValueError(f"{self.mode} mode requires a cassette directory")
            self.cassette_dir.mkdir(parents=True, exist_ok=True)
            if self.mode == REPLAY:
                self._load()

    # -- cassette io ----------------------------------------------------------
    @property
    def _path(self) -> Path:
        assert self.cassette_dir is not None
        return self.cassette_dir / "http.json"

    def _load(self) -> None:
        if not self._path.exists():
            raise ReplayMiss(f"no cassette at {self._path}")
        self._cassette = json.loads(self._path.read_text())

    @property
    def meta_path(self) -> Path:
        assert self.cassette_dir is not None
        return self.cassette_dir / "meta.json"

    def write_meta(self, meta: dict[str, Any]) -> None:
        """Identify the run a cassette came from.

        Replay has to reconstruct the same URLs the recording contains, and
        those URLs embed the repo and the Devin organisation id. Without this,
        replay would build URLs for a placeholder org and miss every key.
        """
        if self.mode != RECORD:
            return
        self.meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")

    def flush(self) -> None:
        if self.mode != RECORD:
            return
        self._path.write_text(json.dumps(self._cassette, indent=2, sort_keys=True))

    # -- request --------------------------------------------------------------
    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        allow_status: tuple[int, ...] = (),
    ) -> Response:
        if params:
            sep = "&" if "?" in url else "?"
            url = url + sep + "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v is not None)
        key = _canonical(method, url, json_body)
        seq = self._counts.get(key, 0)
        self._counts[key] = seq + 1

        if self.mode == REPLAY:
            resp = self._replay(key, seq, method, url)
        else:
            resp = self._live(method, url, headers or {}, json_body)
            if self.mode == RECORD:
                self._cassette.setdefault(key, {"request": {"method": method.upper(), "url": _redact(url)}, "responses": []})
                self._cassette[key]["responses"].append(
                    {"status": resp.status, "body": json.loads(_redact(json.dumps(resp.body, default=str)))}
                )
                self.flush()

        self.calls.append({"method": method.upper(), "url": url, "status": resp.status})
        if not resp.ok and resp.status not in allow_status:
            raise HttpError(method, url, resp.status, json.dumps(resp.body, default=str))
        return resp

    def _live(self, method: str, url: str, headers: dict[str, str], json_body: Any | None) -> Response:
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                r = self._session.request(
                    method.upper(), url, headers=headers, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:  # network flake
                last_exc = exc
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (429, 502, 503, 504) and attempt < 3:
                time.sleep(min(30, 2 ** attempt * 3))
                continue
            try:
                body = r.json() if r.content else None
            except ValueError:
                body = r.text
            return Response(r.status_code, body, dict(r.headers))
        raise RuntimeError(f"{method} {url} failed after retries: {last_exc}")

    def _replay(self, key: str, seq: int, method: str, url: str) -> Response:
        entry = self._cassette.get(key)
        if not entry:
            self.misses.append(f"{method.upper()} {url}")
            raise ReplayMiss(
                f"no recorded response for {method.upper()} {url} (key {key}). "
                "Fixtures are recordings of real runs; replay never invents data."
            )
        responses = entry["responses"]
        item = responses[min(seq, len(responses) - 1)]
        return Response(item["status"], item.get("body"), {})


def read_meta(cassette_dir: str) -> dict[str, Any]:
    """The repo/org a cassette was recorded against, if it declares them."""
    p = Path(cassette_dir) / "meta.json"
    return json.loads(p.read_text()) if p.exists() else {}


def transport_from_env() -> Transport:
    mode = os.environ.get("SWARM_HTTP_MODE", LIVE)
    return Transport(mode=mode, cassette_dir=os.environ.get("SWARM_CASSETTE_DIR"))
