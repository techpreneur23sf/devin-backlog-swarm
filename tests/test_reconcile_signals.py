from swarm.reconcile import ci_status_for


class FakeGH:
    def __init__(self, statuses=(), runs=()):
        self._statuses = list(statuses)
        self._runs = list(runs)

    def combined_status(self, sha):
        return {"state": "success", "total_count": len(self._statuses), "statuses": self._statuses}

    def check_runs(self, sha):
        return {"check_runs": self._runs}


PR = {"head": {"sha": "abc"}}


def test_no_checks_is_none_not_green():
    assert ci_status_for(FakeGH(), PR) == "none"


def test_devin_review_status_alone_is_not_ci():
    """A review is not a test run; a PR must not merge on the reviewer's status."""
    gh = FakeGH(statuses=[{"context": "Devin Review", "state": "success"}])
    assert ci_status_for(gh, PR) == "none"


def test_a_real_check_run_counts():
    gh = FakeGH(
        statuses=[{"context": "Devin Review", "state": "success"}],
        runs=[{"name": "swarm verify", "status": "completed", "conclusion": "success"}],
    )
    assert ci_status_for(gh, PR) == "green"


def test_a_failing_check_is_red_even_beside_successes():
    gh = FakeGH(
        runs=[
            {"name": "swarm verify", "status": "completed", "conclusion": "failure"},
            {"name": "other", "status": "completed", "conclusion": "success"},
        ]
    )
    assert ci_status_for(gh, PR) == "red"


def test_an_incomplete_check_is_pending():
    gh = FakeGH(runs=[{"name": "swarm verify", "status": "in_progress"}])
    assert ci_status_for(gh, PR) == "pending"
