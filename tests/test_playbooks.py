"""Playbook binding: policy names titles, dispatch resolves them to ids."""

from swarm.dispatch import _playbook_id
from swarm.policy import Policy


class FakeDevin:
    def __init__(self, titles):
        self.titles = titles
        self.calls = 0

    def playbook_id_for_title(self, title):
        self.calls += 1
        return self.titles.get(title)


def policy_with(playbooks):
    p = Policy.load(None)
    p.playbooks = playbooks
    return p


def test_a_title_in_policy_is_resolved_to_an_id():
    devin = FakeDevin({"Superset: patch/minor dependency bump": "playbook-abc"})
    p = policy_with({"dep-bump-patch": "Superset: patch/minor dependency bump"})
    assert _playbook_id(devin, p, "dep-bump-patch") == "playbook-abc"


def test_an_explicit_id_is_passed_through_without_a_lookup():
    devin = FakeDevin({})
    p = policy_with({"security": "playbook-explicit"})
    assert _playbook_id(devin, p, "security") == "playbook-explicit"
    assert devin.calls == 0


def test_an_unsynced_playbook_does_not_block_dispatch():
    """The issue carries the task; a missing playbook must not stop the work."""
    devin = FakeDevin({})
    p = policy_with({"deprecation": "Superset: deprecation migration"})
    assert _playbook_id(devin, p, "deprecation") is None


def test_a_class_with_no_playbook_falls_back_to_default():
    devin = FakeDevin({"House rules": "playbook-default"})
    p = policy_with({"default": "House rules"})
    assert _playbook_id(devin, p, "anything-else") == "playbook-default"
