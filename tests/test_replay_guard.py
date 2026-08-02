"""A fixture that no longer covers the code must fail, not replay silently."""

import json

import pytest

from swarm.transport import REPLAY, ReplayMiss, Transport


def _cassette(tmp_path):
    (tmp_path / "http.json").write_text(json.dumps({}))
    return Transport(mode=REPLAY, cassette_dir=str(tmp_path))


def test_an_unrecorded_request_is_recorded_as_a_miss(tmp_path):
    t = _cassette(tmp_path)
    with pytest.raises(ReplayMiss):
        t.request("GET", "https://api.github.com/repos/o/r/pulls/1/reviews")
    assert t.misses == ["GET https://api.github.com/repos/o/r/pulls/1/reviews"]
