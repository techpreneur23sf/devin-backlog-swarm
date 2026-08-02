from swarm.models import DISPATCHED, QUEUED, Task
from swarm.policy import Policy
from swarm.scheduler import plan, scopes_overlap

POLICY = Policy.load("policy.yaml")


def q(number, klass="dep-bump-patch", scope=None):
    return Task(issue_number=number, state=QUEUED, issue_class=klass, touch_scope=scope or ["**"])


def test_scopes_overlap_on_shared_prefix():
    assert scopes_overlap(["requirements/base.txt"], ["requirements/base.txt"])
    assert scopes_overlap(["superset/utils/**"], ["superset/utils/dates.py"])
    assert not scopes_overlap(["superset/utils/dates.py"], ["superset/tasks/cron_util.py"])


def test_whole_repo_scope_serialises_everything():
    assert scopes_overlap(["**"], ["superset/anything.py"])


def test_conflicting_scopes_are_not_dispatched_together():
    tasks = [q(1, scope=["requirements/base.txt"]), q(2, scope=["requirements/base.txt"])]
    go, skipped = plan(tasks, [], POLICY, acus_spent_today=0)
    assert [t.issue_number for t in go] == [1]
    assert skipped and "conflict" in skipped[0][1]


def test_in_flight_scopes_are_held():
    held = Task(issue_number=9, state=DISPATCHED, touch_scope=["superset/utils/**"])
    go, skipped = plan([q(1, scope=["superset/utils/dates.py"])], [held], POLICY, acus_spent_today=0)
    assert not go
    assert "conflict" in skipped[0][1]


def test_concurrency_cap_is_respected():
    tasks = [q(n, scope=[f"superset/m{n}.py"]) for n in range(1, 10)]
    go, skipped = plan(tasks, [], POLICY, acus_spent_today=0)
    assert len(go) == POLICY.budget.max_concurrent_sessions
    assert len(skipped) == len(tasks) - len(go)


def test_daily_acu_cap_stops_dispatch():
    go, skipped = plan([q(1)], [], POLICY, acus_spent_today=POLICY.budget.daily_acu_cap)
    assert not go
    assert "budget" in skipped[0][1].lower() or "acu" in skipped[0][1].lower()


def test_priority_order_follows_policy():
    tasks = [
        q(1, klass="code-quality", scope=["a.py"]),
        q(2, klass="security", scope=["b.py"]),
    ]
    go, _ = plan(tasks, [], POLICY, acus_spent_today=0)
    assert [t.issue_class for t in go][0] == POLICY.class_priority[0] or go[0].issue_number == 2


def test_exhausted_attempts_are_not_redispatched():
    t = q(1)
    t.attempts = POLICY.budget.max_attempts_per_issue
    go, skipped = plan([t], [], POLICY, acus_spent_today=0)
    assert not go
    assert "attempt" in skipped[0][1].lower()
