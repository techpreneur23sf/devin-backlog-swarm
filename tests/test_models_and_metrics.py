from swarm.metrics import compute
from swarm.models import (
    DISPATCHED,
    FAILED,
    MERGED,
    NEEDS_HUMAN,
    PR_OPEN,
    QUEUED,
    RUNNING,
    Ledger,
    Task,
)


def test_transition_records_history_and_reason():
    t = Task(issue_number=1, state=QUEUED)
    t.transition(DISPATCHED, "session created")
    t.transition(RUNNING)
    assert t.state == RUNNING
    assert [h["to"] for h in t.history] == [DISPATCHED, RUNNING]
    assert t.history[0]["reason"] == "session created"


def test_terminal_states_are_terminal():
    assert Task(issue_number=1, state=MERGED).is_terminal
    assert not Task(issue_number=1, state=PR_OPEN).is_terminal


def test_ledger_round_trips_through_json():
    led = Ledger(repo="o/r")
    t = Task(issue_number=3, state=PR_OPEN, pr_number=12, issue_class="deprecation")
    led.upsert(t)
    back = Ledger.from_dict(led.to_dict())
    assert back.get(3).pr_number == 12
    assert back.repo == "o/r"


def test_in_flight_is_sessions_and_active_is_everything_still_moving():
    led = Ledger(repo="o/r")
    for n, s in ((1, QUEUED), (2, RUNNING), (3, MERGED), (4, PR_OPEN)):
        led.upsert(Task(issue_number=n, state=s))
    # a session slot is only occupied while a session is running
    assert {t.issue_number for t in led.in_flight()} == {2}
    # an open PR still has something to observe (and still holds its scope)
    assert {t.issue_number for t in led.active()} == {2, 4}
    assert [t.issue_number for t in led.queued()] == [1]


def test_metrics_report_autonomy_and_merge_rate_from_observed_state():
    led = Ledger(repo="o/r")
    led.upsert(Task(issue_number=1, state=MERGED, issue_class="dep-bump-patch", pr_number=1, session_id="s1", pr_url="u1", merged_by="swarm", acus_consumed=3.0))
    led.upsert(Task(issue_number=2, state=MERGED, issue_class="deprecation", pr_number=2, session_id="s2", pr_url="u2", merged_by="swarm", acus_consumed=5.0))
    led.upsert(Task(issue_number=3, state=NEEDS_HUMAN, issue_class="security", pr_number=3, session_id="s3", pr_url="u3", ever_waited_for_user=True, acus_consumed=2.0))
    led.upsert(Task(issue_number=4, state=FAILED, issue_class="dep-bump-major", session_id="s4", failure_category="verification"))

    m = compute(led)
    assert m["counts"]["merged"] == 2
    assert m["counts"]["needs_human"] == 1
    assert m["acus_total"] == 10.0
    assert m["acus_per_merged_pr"] == 5.0
    assert m["merge_rate"] == 0.5  # two of four dispatched sessions landed
    # three tasks reached a PR; one of them needed a human
    assert round(m["autonomy_rate"], 2) == round(2 / 3, 2)
    assert m["failures"]["verification"] == 1


def test_metrics_on_an_empty_ledger_are_not_invented():
    m = compute(Ledger(repo="o/r"))
    assert m["acus_per_merged_pr"] is None
    assert m["median_issue_to_pr_hours"] is None


def test_an_unmetered_account_reports_no_cost_rather_than_a_free_one():
    """`acus_consumed` is 0.0 on plans Devin does not meter in ACUs.

    Dividing that by the merges would advertise shipped work as free.
    """
    led = Ledger(repo="o/r")
    t = Task(issue_number=1, state=MERGED, issue_class="dep-bump-patch", pr_number=1,
             session_id="s1", pr_url="u1", merged_by="swarm", acus_consumed=0.0)
    t.dispatched_at, t.terminal_at = 1_000_000, 1_000_000 + 3600
    t.session_size, t.devin_messages = "xs", 2
    led.upsert(t)

    m = compute(led)
    assert m["acus_metered"] is False
    assert m["acus_per_merged_pr"] is None
    assert m["acus_on_merged_tasks"] is None
    # the effort signals the API does return on any plan
    assert m["effort"]["session_sizes"] == {"xs": 1}
    assert m["effort"]["devin_messages_total"] == 2
    assert m["effort"]["median_session_hours"] == 1.0
