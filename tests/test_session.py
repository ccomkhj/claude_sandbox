import json
import time

import pytest

from sandbox import session


def test_new_session_creates_dir_and_meta(sandbox_home):
    meta = session.new_session(goal="refactor X", repo="/tmp/myrepo")
    assert meta.status == "starting"
    assert meta.goal == "refactor X"
    assert meta.repo == "/tmp/myrepo"
    assert meta.branch == f"sandbox/{meta.id}"
    assert meta.started_at <= time.time()

    sdir = session.session_dir(meta.id)
    assert sdir.is_dir()
    on_disk = json.loads((sdir / "meta.json").read_text())
    assert on_disk["id"] == meta.id
    assert on_disk["status"] == "starting"


def test_load_round_trips(sandbox_home):
    meta = session.new_session(goal="g", repo="/tmp/r")
    loaded = session.load(meta.id)
    assert loaded == meta


def test_save_overwrites(sandbox_home):
    meta = session.new_session(goal="g", repo="/tmp/r")
    meta.status = "running"
    session.save(meta)
    assert session.load(meta.id).status == "running"


def test_find_by_prefix(sandbox_home, monkeypatch):
    monkeypatch.setattr(session, "_new_id", lambda: "AAAAAA" + "0" * 20)
    a = session.new_session(goal="a", repo="/tmp/r")
    monkeypatch.setattr(session, "_new_id", lambda: "BBBBBB" + "0" * 20)
    b = session.new_session(goal="b", repo="/tmp/r")
    found = session.find(a.id[:6])
    assert found.id == a.id
    assert found.id != b.id


def test_find_ambiguous_prefix_raises(sandbox_home, monkeypatch):
    monkeypatch.setattr(session, "_new_id", lambda: "AABBCC" + "0" * 20)
    a = session.new_session(goal="a", repo="/tmp/r")
    monkeypatch.setattr(session, "_new_id", lambda: "AABBCD" + "0" * 20)
    b = session.new_session(goal="b", repo="/tmp/r")
    with pytest.raises(LookupError, match="ambiguous"):
        session.find("AABB")


def test_find_no_match_raises(sandbox_home):
    with pytest.raises(LookupError, match="no session"):
        session.find("ZZZZ")


def test_find_no_match_with_other_sessions(sandbox_home):
    session.new_session(goal="g", repo="/tmp/r")  # ensures sessions/ exists
    with pytest.raises(LookupError, match="no session"):
        session.find("ZZZZ")
