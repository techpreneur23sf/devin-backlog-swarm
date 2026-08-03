from swarm.models import DISPATCHED, QUEUED, Ledger, Task, now
from swarm.policy import Policy
from swarm.scheduler import plan, reserved_acus_today, scopes_overlap

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
    assert "ACU cap" in skipped[0][1].lower() or "acu" in skipped[0][1].lower()


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


def test_the_daily_cap_binds_even_when_the_account_reports_no_acus():
    """An unmetered plan reports 0.0 ACUs forever; reservations still cap the day."""
    ledger = Ledger(repo="owner/name")
    per_task = POLICY.acu_limit("dep-bump-patch")
    count = int(POLICY.budget.daily_acu_cap // per_task) + 1
    for n in range(count):
        t = Task(issue_number=n, state=DISPATCHED, issue_class="dep-bump-patch")
        t.dispatched_at = now()
        t.acus_consumed = 0.0  # what the API reports on this plan
        ledger.upsert(t)

    reserved = reserved_acus_today(ledger, POLICY)
    assert reserved == count * per_task > POLICY.budget.daily_acu_cap

    go, skipped = plan([q(99, scope=["superset/x.py"])], [], POLICY, acus_spent_today=reserved)
    assert not go
    assert "ACU cap" in skipped[0][1]


def test_observed_consumption_wins_when_it_exceeds_the_reservation():
    ledger = Ledger(repo="owner/name")
    t = Task(issue_number=1, state=DISPATCHED, issue_class="dep-bump-patch")
    t.dispatched_at = now()
    t.acus_consumed = 99.0
    ledger.upsert(t)
    assert reserved_acus_today(ledger, POLICY) == 99.0


def test_yesterdays_dispatches_do_not_spend_todays_budget():
    ledger = Ledger(repo="owner/name")
    t = Task(issue_number=1, state=DISPATCHED, issue_class="dep-bump-patch")
    t.dispatched_at = now() - 2 * 86400
    ledger.upsert(t)
    assert reserved_acus_today(ledger, POLICY) == 0.0
