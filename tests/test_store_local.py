from pathlib import Path

import pytest

from sk8s.errors import BlobNotFound
from sk8s.store import get_store
from sk8s.store.local import LocalStore


def test_roundtrip(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    uri = s.put("wf-a/job-b/payload", b"hello")
    assert uri == s.uri_for("wf-a/job-b/payload")
    assert s.get(uri) == b"hello"


def test_get_missing_raises(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("nope/missing"))


def test_delete_idempotent(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    uri = s.put("k", b"x")
    s.delete(uri)
    s.delete(uri)  # no error
    with pytest.raises(BlobNotFound):
        s.get(uri)


def test_delete_prefix(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put("wf-a/j1/r", b"1")
    s.put("wf-a/j2/r", b"2")
    s.put("wf-b/j1/r", b"3")
    s.delete_prefix("wf-a")
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("wf-a/j1/r"))
    assert s.get(s.uri_for("wf-b/j1/r")) == b"3"


def test_delete_prefix_matches_whole_segments(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put("wf-a/j1/r", b"1")
    s.put("wf-a/j1-x/r", b"3")
    s.put("wf-ab/j1/r", b"2")
    # multi-segment prefix: wf-a/j1 does not take wf-a/j1-x
    s.delete_prefix("wf-a/j1")
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("wf-a/j1/r"))
    assert s.get(s.uri_for("wf-a/j1-x/r")) == b"3"
    # single-segment prefix: wf-a does not take wf-ab
    s.delete_prefix("wf-a")
    with pytest.raises(BlobNotFound):
        s.get(s.uri_for("wf-a/j1-x/r"))
    assert s.get(s.uri_for("wf-ab/j1/r")) == b"2"


def test_delete_prefix_rejects_empty(tmp_path: Path) -> None:
    s = LocalStore(tmp_path)
    s.put("wf-a/j1/r", b"1")
    with pytest.raises(ValueError):
        s.delete_prefix("/")  # would otherwise rmtree the whole root
    assert s.get(s.uri_for("wf-a/j1/r")) == b"1"


def test_factory(tmp_path: Path) -> None:
    s = get_store(f"local:{tmp_path}")
    assert isinstance(s, LocalStore)
