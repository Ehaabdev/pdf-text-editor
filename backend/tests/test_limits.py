"""The limits that keep a small public server usable: per visitor, per file, in total."""

from app import config, limits
from conftest import make_pdf, upload


def test_a_visitor_who_opens_too_many_files_is_asked_to_wait(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_UPLOADS_PER_HOUR", 3)
    for _ in range(3):
        assert upload(client, make_pdf()).status_code == 201
    r = upload(client, make_pdf())
    assert r.status_code == 429 and "Wait" in r.json()["detail"]


def test_too_many_changes_in_a_short_time(client, monkeypatch):
    monkeypatch.setattr(config, "RATE_CHANGES_PER_HOUR", 2)
    doc = upload(client, make_pdf()).json()
    for _ in range(2):
        client.post(f"/api/documents/{doc['id']}/undo", json={"revision": doc["revision"]})
    r = client.post(f"/api/documents/{doc['id']}/undo", json={"revision": doc["revision"]})
    assert r.status_code == 429


def test_the_window_moves_on():
    limiter = limits.RateLimiter()
    limit, window = limiter.rule("read")
    assert all(limiter.allow("a", "read", now=0.0) for _ in range(limit))
    assert not limiter.allow("a", "read", now=1.0)
    assert limiter.allow("b", "read", now=1.0)  # someone else
    assert limiter.allow("a", "read", now=window + 0.5)


def test_requests_are_told_apart():
    assert limits.kind_of("POST", "/api/documents") == "upload"
    assert limits.kind_of("POST", "/api/documents/abc/edits") == "change"
    assert limits.kind_of("POST", "/api/documents/abc/versions/3") == "change"
    assert limits.kind_of("GET", "/api/documents/abc/pages/0/image") == "read"
    assert limits.kind_of("GET", "/") is None


def test_a_file_keeps_only_so_many_changes(client, monkeypatch):
    from app.main import store

    monkeypatch.setattr(config, "MAX_VERSIONS", 2)
    doc = upload(client, make_pdf()).json()
    data = (store.current_path(store.get(doc["id"]))).read_bytes()
    meta = store.save_version(doc["id"], doc["revision"], data)
    meta = store.save_version(doc["id"], meta.revision, data)
    try:
        store.save_version(doc["id"], meta.revision, data)
        raise AssertionError("a third change was kept")
    except Exception as e:
        assert type(e).__name__ == "TooManyChanges"


def test_a_full_server_says_so(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOTAL_BYTES", 1)
    r = upload(client, make_pdf())
    assert r.status_code == 503 and "full" in r.json()["detail"]
