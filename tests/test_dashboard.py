"""The dashboard is the artefact a VP reads, so it may not overstate the data."""

from swarm.dashboard import render
from swarm.models import MERGED, Ledger, Task


def _ledger() -> Ledger:
    led = Ledger()
    led.tasks[1] = Task(
        issue_number=1,
        issue_title="Bump flask",
        issue_class="dep-bump-patch",
        state=MERGED,
        pr_number=2,
        pr_url="https://github.com/o/r/pull/2",
        ci_status="green",
        review_status="clean",
    )
    return led


def test_unmetered_cost_is_absent_rather_than_shown_as_an_empty_card():
    """A card reading "not metered" spends headline space to report nothing."""
    html = render(_ledger(), "o/r")
    assert "not metered" not in html
    assert "Cost per merged PR" not in html
    assert "ACUs per merged PR" not in html
    # It is still explained, once, in prose.
    assert "does not report one" in html


def test_ci_and_review_render_as_coloured_dots():
    html = render(_ledger(), "o/r")
    assert 'class="dot" style="background:#16a34a"' in html


def test_titles_are_escaped():
    led = _ledger()
    led.tasks[1].issue_title = '<img src=x onerror="alert(1)">'
    assert "<img src=x" not in render(led, "o/r")
