"""A parked task's PR still belongs to GitHub, not to the ledger."""

from swarm.metrics import compute
from swarm.models import MERGED, NEEDS_HUMAN, Ledger, Task
from swarm.policy import Policy
from swarm.reconcile import reconcile

POLICY = Policy.load("policy.yaml")


class FakeGH:
    repo = "owner/name"

    def __init__(self, pr):
        self._pr = pr
        self.labels = []

    def get_pr(self, number):
        return self._pr

    def set_labels(self, issue, labels):
        self.labels = labels

    def list_labels(self, issue):
        return []

    def add_labels(self, issue, labels):
        self.labels += labels

    def remove_label(self, issue, label):
        pass


class FakeDevin:
    def get_session(self, sid):
        return {}


def _parked_task():
    t = Task(issue_number=5, issue_class="code-quality", state=NEEDS_HUMAN)
    t.pr_url = "https://github.com/owner/name/pull/22"
    t.pr_number = 22
    return t


def _ledger(task):
    led = Ledger(repo="owner/name")
    led.upsert(task)
    return led


def test_a_human_merging_a_parked_pr_is_observed_as_a_merge():
    task = _parked_task()
    gh = FakeGH({"merged": True, "state": "closed", "head": {"sha": "abc"}})
    reconcile(gh, FakeDevin(), _ledger(task), POLICY, dry_run=True)
    assert task.state == MERGED
    assert task.merged_by == "human"


def test_a_human_merge_does_not_count_towards_autonomy():
    task = _parked_task()
    ledger = _ledger(task)
    reconcile(FakeGH({"merged": True, "state": "closed", "head": {"sha": "abc"}}), FakeDevin(), ledger, POLICY, dry_run=True)
    counts = compute(ledger)["counts"]
    assert counts["merged"] == 1
    assert counts["merged_autonomously"] == 0
    assert counts["merged_by_human"] == 1


def test_a_parked_pr_that_is_still_open_stays_parked():
    task = _parked_task()
    reconcile(FakeGH({"merged": False, "state": "open", "head": {"sha": "abc"}}), FakeDevin(), _ledger(task), POLICY, dry_run=True)
    assert task.state == NEEDS_HUMAN
