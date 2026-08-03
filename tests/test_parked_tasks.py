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

    def get_insights(self, sid):
        return {"session_size": "xs", "num_devin_messages": 2, "acus_consumed": 0.0}


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
    assert counts["merged_by_swarm"] == 0
    assert counts["merged_by_human"] == 1


def test_a_parked_pr_that_is_still_open_stays_parked():
    task = _parked_task()
    reconcile(FakeGH({"merged": False, "state": "open", "head": {"sha": "abc"}}), FakeDevin(), _ledger(task), POLICY, dry_run=True)
    assert task.state == NEEDS_HUMAN


def test_merge_actor_is_recovered_from_history_for_older_entries():
    """Ledgers written before `merged_by` existed still recorded who merged."""
    older = {
        "issue_number": 13,
        "state": MERGED,
        "history": [{"at": 1, "from": "review_clean", "to": MERGED, "reason": "satisfies tier 'auto_merge'"}],
    }
    assert Task.from_dict(older).merged_by == "swarm"
    human = dict(older, history=[{"at": 1, "from": "needs_human", "to": MERGED, "reason": "pull request merged by a human"}])
    assert Task.from_dict(human).merged_by == "human"
    unknown = dict(older, history=[{"at": 1, "from": "pr_open", "to": MERGED, "reason": "pull request merged"}])
    assert Task.from_dict(unknown).merged_by is None


def test_a_suspended_session_with_no_pr_does_not_hold_its_touch_scope_forever():
    task = Task(issue_number=11, issue_class="dep-bump-patch", state="dispatched", session_id="s11")
    ledger = _ledger(task)

    class Suspended(FakeDevin):
        def get_session(self, sid):
            return {"status": "suspended", "status_detail": "inactivity"}

    reconcile(FakeGH({}), Suspended(), ledger, POLICY, dry_run=True)
    assert task.state == "failed"
    assert task not in ledger.in_flight()


def test_an_idle_session_is_not_human_involvement():
    """A finished session sits in waiting_for_user because nobody is talking to it."""
    landed = Task(issue_number=20, state=MERGED, merged_by="swarm", ever_waited_for_user=True)
    assert not landed.needed_a_human

    parked_then_merged = Task(issue_number=21, state=MERGED, merged_by="swarm")
    parked_then_merged.history = [{"at": 1, "from": "pr_open", "to": NEEDS_HUMAN, "reason": "review findings"}]
    assert parked_then_merged.needed_a_human
