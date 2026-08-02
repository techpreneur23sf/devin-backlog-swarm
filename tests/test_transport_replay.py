import json

import pytest

from swarm.transport import REPLAY, ReplayMiss, Response, Transport, _canonical, _redact


def _cassette(tmp_path, method, url, body, status=200):
    key = _canonical(method, url, None)
    (tmp_path / "http.json").write_text(
        json.dumps({key: {"request": {"method": method, "url": url}, "responses": [{"status": status, "body": body}]}})
    )


def test_replay_serves_the_recorded_response(tmp_path):
    _cassette(tmp_path, "GET", "https://api.github.com/repos/o/r", {"full_name": "o/r"})
    t = Transport(mode=REPLAY, cassette_dir=str(tmp_path))
    assert t.request("GET", "https://api.github.com/repos/o/r").json()["full_name"] == "o/r"


def test_replay_fails_loudly_on_an_unrecorded_request(tmp_path):
    _cassette(tmp_path, "GET", "https://api.github.com/repos/o/r", {"full_name": "o/r"})
    t = Transport(mode=REPLAY, cassette_dir=str(tmp_path))
    with pytest.raises(ReplayMiss) as exc:
        t.request("GET", "https://api.devin.ai/v3/self")
    assert "never invents" in str(exc.value)


def test_replay_makes_no_network_call(tmp_path):
    class Exploding:
        def request(self, *a, **k):  # pragma: no cover - must never run
            raise AssertionError("replay mode reached the network")

    _cassette(tmp_path, "GET", "https://api.devin.ai/v3/self", {"user_id": "u"})
    t = Transport(mode=REPLAY, cassette_dir=str(tmp_path), session=Exploding())
    assert t.request("GET", "https://api.devin.ai/v3/self").json() == {"user_id": "u"}


def test_repeated_calls_walk_the_recorded_sequence(tmp_path):
    """The reconciler polls the same URL repeatedly; replay must show it changing."""
    key = _canonical("GET", "https://api.devin.ai/v3/sessions/x", None)
    (tmp_path / "http.json").write_text(
        json.dumps(
            {
                key: {
                    "request": {"method": "GET", "url": "https://api.devin.ai/v3/sessions/x"},
                    "responses": [
                        {"status": 200, "body": {"status": "running"}},
                        {"status": 200, "body": {"status": "finished"}},
                    ],
                }
            }
        )
    )
    t = Transport(mode=REPLAY, cassette_dir=str(tmp_path))
    assert t.request("GET", "https://api.devin.ai/v3/sessions/x").json()["status"] == "running"
    assert t.request("GET", "https://api.devin.ai/v3/sessions/x").json()["status"] == "finished"
    # exhausted sequences hold at the last observation rather than inventing one
    assert t.request("GET", "https://api.devin.ai/v3/sessions/x").json()["status"] == "finished"


def test_missing_cassette_is_an_error_not_an_empty_run(tmp_path):
    with pytest.raises(ReplayMiss):
        Transport(mode=REPLAY, cassette_dir=str(tmp_path / "nope"))


def test_tokens_are_redacted_before_they_reach_a_fixture():
    assert "ghp_abcdef123456" not in _redact("Authorization: ghp_abcdef123456")
    assert "REDACTED" in _redact("token ghp_abcdef123456")


def test_response_ok_reflects_status():
    assert Response(200, {}, {}).ok
    assert not Response(404, {}, {}).ok
