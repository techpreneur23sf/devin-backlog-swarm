from swarm.verify import CLOSES_RE, scope_violations


def test_scope_violations_flags_files_outside_the_declared_scope():
    assert scope_violations(
        ["superset/utils/dates.py", "superset/models/helpers.py"], ["superset/utils/dates.py"]
    ) == ["superset/models/helpers.py"]


def test_glob_scopes_cover_their_subtree():
    assert not scope_violations(["superset/utils/dates.py"], ["superset/utils/**"])


def test_whole_repo_scope_never_violates():
    assert not scope_violations(["anything.py"], ["**"])


def test_closes_reference_is_found_case_insensitively():
    assert CLOSES_RE.search("Closes #12").group(1) == "12"
    assert CLOSES_RE.search("this fixes #7 nicely").group(1) == "7"
    assert CLOSES_RE.search("no reference here") is None
