import json

from swarm.issues import IssueSpec, render_issue_body
from swarm.scan import Finding, collapse, parse_osv, run_scanners


def _osv(package, version, ids, source="requirements/base.txt", fixed="9.9.9"):
    return {
        "results": [
            {
                "source": {"path": source},
                "packages": [
                    {
                        "package": {"name": package, "version": version, "ecosystem": "PyPI"},
                        "vulnerabilities": [
                            {
                                "id": i,
                                "summary": f"{i} summary",
                                "affected": [
                                    {
                                        "package": {"name": package},
                                        "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": fixed}]}],
                                    }
                                ],
                            }
                            for i in ids
                        ],
                    }
                ],
            }
        ]
    }


def test_parse_osv_reads_package_version_and_fix():
    findings = parse_osv(json.dumps(_osv("flask", "2.3.3", ["PYSEC-1"])))
    assert len(findings) == 1
    f = findings[0]
    assert (f.package, f.current_version, f.fixed_version, f.file) == ("flask", "2.3.3", "9.9.9", "base.txt")


def test_aliased_advisories_collapse_into_one_issue():
    findings = parse_osv(json.dumps(_osv("mcp", "1.24.0", ["PYSEC-1", "GHSA-2", "GHSA-3"])))
    collapsed = collapse(findings)
    assert len(collapsed) == 1
    assert collapsed[0].aliases == ["GHSA-2", "GHSA-3"]


def test_one_package_pinned_in_two_files_is_one_task():
    findings = parse_osv(json.dumps(_osv("flask", "2.3.3", ["PYSEC-1"])))
    findings += parse_osv(json.dumps(_osv("flask", "2.3.3", ["PYSEC-1"], source="requirements/development.txt")))
    collapsed = collapse(findings)
    assert len(collapsed) == 1
    assert sorted(collapsed[0].files) == ["base.txt", "development.txt"]


def test_a_major_bump_is_classified_differently_from_a_patch_bump():
    major = collapse(parse_osv(json.dumps(_osv("flask", "2.3.3", ["PYSEC-1"], fixed="3.1.3"))))[0]
    patch = collapse(parse_osv(json.dumps(_osv("flask", "2.3.3", ["PYSEC-1"], fixed="2.3.9"))))[0]
    assert major.issue_class == "dep-bump-major"
    assert patch.issue_class == "dep-bump-patch"


def test_fingerprint_is_stable_across_advisory_churn():
    a = Finding("osv-scanner", "PyPI", "flask", "PYSEC-1", "high", "2.3.3", "3.0.0", "base.txt", "")
    b = Finding("osv-scanner", "PyPI", "flask", "GHSA-9", "high", "2.3.3", "3.1.0", "base.txt", "")
    assert a.fingerprint == b.fingerprint


def test_issue_metadata_survives_a_round_trip():
    body = render_issue_body(
        problem="p",
        affected_paths=["superset/utils/dates.py"],
        acceptance=["a"],
        verify=["pytest tests/unit_tests/utils -q"],
        issue_class="deprecation",
        touch_scope=["superset/utils/dates.py"],
        fingerprint="abc123",
        tier="tier-2",
    )
    spec = IssueSpec.from_issue({"number": 7, "title": "t", "body": body, "labels": [{"name": "class:deprecation"}]})
    assert spec.issue_class == "deprecation"
    assert spec.touch_scope == ["superset/utils/dates.py"]
    assert spec.verify == ["pytest tests/unit_tests/utils -q"]
    assert spec.fingerprint == "abc123"


def test_an_issue_without_metadata_gets_the_widest_scope():
    spec = IssueSpec.from_issue({"number": 8, "title": "t", "body": "just prose", "labels": []})
    assert spec.touch_scope == ["**"]
    assert spec.verify == []


def test_a_scanner_that_never_ran_is_reported_not_silently_skipped(tmp_path, monkeypatch):
    """An empty backlog must not be indistinguishable from a clean repo."""
    monkeypatch.setattr("swarm.scan.shutil.which", lambda _: None)
    findings, report = run_scanners(str(tmp_path), tools=["semgrep"])
    assert findings == []
    assert [(r.tool, r.status) for r in report] == [("semgrep", "missing")]
    # nothing claimed to be ok, so `swarm scan` exits nonzero on this
    assert not any(r.status == "ok" for r in report)


def test_a_missing_binary_is_reported_as_missing(tmp_path):
    from swarm.scan import _run

    report = []
    assert _run(["definitely-not-a-real-binary"], str(tmp_path), report) is None
    assert report[0].status == "missing"


def test_two_scanners_reporting_the_same_pin_are_one_task():
    """Adding pip-audit alongside osv-scanner must not refile tracked work."""
    osv = Finding("osv-scanner", "PyPI", "flask", "PYSEC-1", "moderate", "2.3.3", "3.1.3", "base.txt", "x")
    pa = Finding("pip-audit", "PyPI", "flask", "GHSA-2", "moderate", "2.3.3", "3.1.3", "base.txt", "x")
    out = collapse([osv, pa])
    assert len(out) == 1
    assert out[0].tools == ["osv-scanner", "pip-audit"]
    assert out[0].aliases == ["GHSA-2"]
    assert osv.fingerprint == pa.fingerprint


def test_an_issue_filed_before_the_key_changed_is_still_recognised():
    """The old key included the tool; those issues must not be refiled."""
    f = Finding("pip-audit", "PyPI", "flask", "PYSEC-1", "moderate", "2.3.3", "3.1.3", "base.txt", "x")
    legacy_osv = f.legacy_fingerprints[0]
    assert legacy_osv != f.fingerprint

    class Gh:
        repo = "o/r"

        def list_issues(self, state="all"):
            return [{"number": 10, "body": f"<!-- swarm-finding: {legacy_osv} -->"}]

        def create_issue(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("refiled an issue that already exists")

    from swarm.scan import file_issues

    assert file_issues(Gh(), [f]) == []


def test_low_impact_semgrep_hits_are_not_filed():
    """`p/python` reports plenty of style-grade hits; filing them buries the rest."""
    from swarm.scan import parse_semgrep

    out = json.dumps(
        {
            "results": [
                {"path": "a.py", "check_id": "x.insecure-hash", "extra": {"metadata": {"impact": "MEDIUM"}, "message": "md5"}},
                {"path": "b.py", "check_id": "x.nitpick", "extra": {"metadata": {"impact": "LOW"}, "message": "meh"}},
            ]
        }
    )
    findings = parse_semgrep(out)
    assert [f.advisory for f in findings] == ["insecure-hash"]
    assert findings[0].issue_class == "security"
    assert len(parse_semgrep(out, min_impact=False)) == 2
