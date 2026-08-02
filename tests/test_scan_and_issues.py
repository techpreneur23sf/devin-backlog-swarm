import json

from swarm.issues import IssueSpec, render_issue_body
from swarm.scan import Finding, collapse, parse_osv


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
