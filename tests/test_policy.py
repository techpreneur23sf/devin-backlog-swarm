from swarm.policy import Policy, evaluate_merge

POLICY = Policy.load("policy.yaml")


def _args(**over):
    base = dict(
        issue_class="dep-bump-patch",
        ci_status="green",
        review_status="clean",
        structured_output={
            "outcome": "fixed",
            "confidence": "high",
            "verification_passed": True,
            "blockers": [],
        },
        files_changed=["requirements/base.txt"],
    )
    base.update(over)
    return base


def test_clean_patch_bump_auto_merges():
    assert evaluate_merge(POLICY, **_args()).allowed


def test_security_class_is_never_auto_merged():
    d = evaluate_merge(POLICY, **_args(issue_class="security", files_changed=["superset/examples/utils.py"]))
    assert not d.allowed
    assert "human" in d.reason


def test_absent_ci_is_not_green_ci():
    assert not evaluate_merge(POLICY, **_args(ci_status="none")).allowed
    assert not evaluate_merge(POLICY, **_args(ci_status=None)).allowed


def test_pending_review_blocks_merge():
    assert not evaluate_merge(POLICY, **_args(review_status="pending")).allowed


def test_low_confidence_blocks_merge():
    so = dict(_args()["structured_output"], confidence="low")
    assert not evaluate_merge(POLICY, **_args(structured_output=so)).allowed


def test_blockers_block_merge():
    so = dict(_args()["structured_output"], blockers=["could not run the tests"])
    assert not evaluate_merge(POLICY, **_args(structured_output=so)).allowed


def test_partial_outcome_blocks_merge():
    so = dict(_args()["structured_output"], outcome="partial")
    assert not evaluate_merge(POLICY, **_args(structured_output=so)).allowed


def test_failed_verification_blocks_merge():
    so = dict(_args()["structured_output"], verification_passed=False)
    assert not evaluate_merge(POLICY, **_args(structured_output=so)).allowed


def test_unknown_class_falls_back_to_a_human():
    assert not evaluate_merge(POLICY, **_args(issue_class="mystery")).allowed


def test_kill_switch_blocks_everything():
    p = Policy.load("policy.yaml")
    p.kill_switch = True
    assert not evaluate_merge(p, **_args()).allowed


def test_env_overrides_budget_and_kill_switch(monkeypatch):
    monkeypatch.setenv("SWARM_MAX_SESSIONS", "2")
    monkeypatch.setenv("SWARM_KILL_SWITCH", "true")
    p = Policy.load("policy.yaml")
    assert p.budget.max_concurrent_sessions == 2
    assert p.kill_switch
