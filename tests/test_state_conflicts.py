"""A concurrent writer must not be able to revert tasks it never touched."""

from __future__ import annotations

import base64
import json

from swarm.models import MERGED, QUEUED, RUNNING, Ledger, Task
from swarm.state import STATE_PATH, StateStore


class FakeGitHub:
    """Enough of the contents API to exercise compare-and-swap."""

    repo = "owner/repo"

    def __init__(self, ledger: Ledger) -> None:
        self.content = json.dumps(ledger.to_dict())
        self.sha = "sha-0"
        self.writes = 0

    def get_file(self, path: str, branch: str) -> dict[str, str]:
        assert path == STATE_PATH
        return {"sha": self.sha, "content": base64.b64encode(self.content.encode()).decode()}

    def put_file(self, path: str, branch: str, content: str, message: str, sha: str | None = None):
        if sha != self.sha:
            return {"message": "does not match"}  # what GitHub returns on a 409
        self.writes += 1
        self.content = content
        self.sha = f"sha-{self.writes}"
        return {"content": {"sha": self.sha}}


def _ledger(**states: str) -> Ledger:
    led = Ledger(repo="owner/repo")
    for key, state in states.items():
        led.upsert(Task(issue_number=int(key.lstrip("i")), state=state))
    return led


def test_conflicting_writer_keeps_the_winners_transitions() -> None:
    remote = FakeGitHub(_ledger(i1=QUEUED, i2=QUEUED))

    stale = StateStore(remote)          # a dispatch job that read early
    stale_ledger = stale.load()

    winner = StateStore(remote)         # a reconcile tick that read later
    winner_ledger = winner.load()
    winner_ledger.get(1).state = MERGED
    winner.save(winner_ledger)

    # The stale writer only ever touched #2.
    stale_ledger.get(2).state = RUNNING
    stale.save(stale_ledger)

    final = Ledger.from_dict(json.loads(remote.content))
    assert final.get(2).state == RUNNING, "its own change must land"
    assert final.get(1).state == MERGED, "the other task must not be reverted"


def test_uncontended_write_is_a_single_put() -> None:
    remote = FakeGitHub(_ledger(i1=QUEUED))
    store = StateStore(remote)
    ledger = store.load()
    ledger.get(1).state = RUNNING
    store.save(ledger)
    assert remote.writes == 1
