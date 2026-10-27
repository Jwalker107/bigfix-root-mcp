"""tools/ingest/fetch.py: manifest sync against a fake HTTP layer (no network)."""

import hashlib
from datetime import date

import pytest
import requests

from tools.ingest import fetch

PAGE = "https://docs.example.com/guide.html"
PDF = "https://docs.example.com/ref.pdf"
TODAY = date(2026, 1, 2)


class FakeHTTP:
    """Stands in for a streamed requests.Response."""

    def __init__(self, status=200, body=b"", headers=None) -> None:
        self.status_code = status
        self.body = body
        self.headers = headers or {}
        self.closed = False

    @property
    def text(self) -> str:
        return self.body.decode()

    def iter_content(self, chunk_size):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start : start + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """url -> response (or exception); the last queued entry repeats."""

    def __init__(self, routes=None) -> None:
        self.routes = {url: list(items) for url, items in (routes or {}).items()}
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None, stream=False):
        self.calls.append(url)
        queue = self.routes.get(url)
        if queue is None:
            if url.endswith("/robots.txt"):
                return FakeHTTP(404)
            raise AssertionError(f"unexpected request: {url}")
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_fetcher(session, **kwargs):
    sleeps: list[float] = []
    kwargs.setdefault("delay", 0)
    fetcher = fetch.Fetcher(session, sleep=sleeps.append, clock=lambda: 0.0, **kwargs)
    fetcher.sleeps = sleeps  # type: ignore[attr-defined]
    return fetcher


def row(source_id="guide", url=PAGE, **extra):
    base = dict.fromkeys(fetch.MANIFEST_COLUMNS, "")
    base.update(id=source_id, source_url=url, **extra)
    return base


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class TestSync:
    def test_new_source_is_written_and_recorded(self, tmp_path):
        session = FakeSession({PAGE: [FakeHTTP(body=b"<html>one</html>")]})
        rows = [row()]
        results = fetch.sync(rows, tmp_path, make_fetcher(session), today=TODAY)
        assert [(r.source_id, r.status) for r in results] == [("guide", "new")]
        assert (tmp_path / "guide.html").read_bytes() == b"<html>one</html>"
        assert rows[0]["sha256"] == sha(b"<html>one</html>")
        assert rows[0]["path"] == "guide.html"
        assert rows[0]["retrieved_on"] == "2026-01-02"

    def test_unchanged_source_leaves_manifest_alone(self, tmp_path):
        body = b"same"
        (tmp_path / "guide.html").write_bytes(body)
        session = FakeSession({PAGE: [FakeHTTP(body=body)]})
        rows = [row(sha256=sha(body), path="guide.html", retrieved_on="2025-12-01")]
        results = fetch.sync(rows, tmp_path, make_fetcher(session), today=TODAY)
        assert results[0].status == "unchanged"
        assert rows[0]["retrieved_on"] == "2025-12-01"

    def test_unchanged_source_restores_a_missing_local_copy(self, tmp_path):
        body = b"same"
        session = FakeSession({PAGE: [FakeHTTP(body=body)]})
        rows = [row(sha256=sha(body))]
        results = fetch.sync(rows, tmp_path, make_fetcher(session), today=TODAY)
        assert results[0].status == "unchanged"
        assert "restored" in results[0].detail
        assert (tmp_path / "guide.html").read_bytes() == body
        assert rows[0]["retrieved_on"] == ""

    def test_changed_source_updates_hash_and_file(self, tmp_path):
        (tmp_path / "guide.html").write_bytes(b"old")
        session = FakeSession({PAGE: [FakeHTTP(body=b"new")]})
        rows = [row(sha256=sha(b"old"), path="guide.html")]
        results = fetch.sync(rows, tmp_path, make_fetcher(session), today=TODAY)
        assert results[0].status == "changed"
        assert (tmp_path / "guide.html").read_bytes() == b"new"
        assert rows[0]["sha256"] == sha(b"new")

    def test_pdf_keeps_its_extension_and_unknown_gets_bin(self, tmp_path):
        session = FakeSession(
            {PDF: [FakeHTTP(body=b"%PDF")], "https://docs.example.com/x": [FakeHTTP(body=b"x")]}
        )
        rows = [row("ref", PDF), row("plain", "https://docs.example.com/x")]
        fetch.sync(rows, tmp_path, make_fetcher(session), today=TODAY)
        assert rows[0]["path"] == "ref.pdf"
        assert rows[1]["path"] == "plain.bin"

    def test_dry_run_writes_nothing(self, tmp_path):
        session = FakeSession({PAGE: [FakeHTTP(body=b"data")]})
        rows = [row()]
        results = fetch.sync(rows, tmp_path / "raw", make_fetcher(session), dry_run=True)
        assert results[0].status == "new"
        assert not (tmp_path / "raw").exists()
        assert rows[0]["sha256"] == ""

    def test_gone_is_reported_and_existing_file_kept(self, tmp_path):
        (tmp_path / "guide.html").write_bytes(b"keep me")
        session = FakeSession({PAGE: [FakeHTTP(404)]})
        rows = [row(sha256=sha(b"keep me"), path="guide.html")]
        results = fetch.sync(rows, tmp_path, make_fetcher(session))
        assert results[0].status == "gone"
        assert (tmp_path / "guide.html").read_bytes() == b"keep me"

    def test_robots_disallow_blocks_without_fetching_the_page(self, tmp_path):
        robots = FakeHTTP(body=b"User-agent: *\nDisallow: /")
        session = FakeSession({"https://docs.example.com/robots.txt": [robots]})
        results = fetch.sync([row()], tmp_path, make_fetcher(session))
        assert results[0].status == "blocked"
        assert PAGE not in session.calls

    def test_robots_server_error_is_treated_as_disallow(self, tmp_path):
        session = FakeSession({"https://docs.example.com/robots.txt": [FakeHTTP(503)]})
        results = fetch.sync([row()], tmp_path, make_fetcher(session))
        assert results[0].status == "blocked"

    def test_robots_is_fetched_once_per_origin(self, tmp_path):
        session = FakeSession({PAGE: [FakeHTTP(body=b"a")], PDF: [FakeHTTP(body=b"b")]})
        rows = [row("guide"), row("ref", PDF)]
        fetch.sync(rows, tmp_path, make_fetcher(session))
        assert session.calls.count("https://docs.example.com/robots.txt") == 1

    def test_only_limits_the_run(self, tmp_path):
        session = FakeSession({PAGE: [FakeHTTP(body=b"a")]})
        rows = [row("guide"), row("ref", PDF)]
        results = fetch.sync(rows, tmp_path, make_fetcher(session), only={"guide"})
        assert [r.source_id for r in results] == ["guide"]

    @pytest.mark.parametrize("bad", ["../escape.html", "/abs/escape.html", "a/../../escape.html"])
    def test_path_escaping_the_raw_dir_is_an_error(self, tmp_path, bad):
        session = FakeSession({PAGE: [FakeHTTP(body=b"x")]})
        results = fetch.sync([row(path=bad)], tmp_path / "raw", make_fetcher(session))
        assert results[0].status == "error"
        assert "escapes" in results[0].detail
        assert not (tmp_path / "escape.html").exists()

    @pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "no-scheme"])
    def test_non_http_urls_are_refused(self, tmp_path, url):
        session = FakeSession()
        results = fetch.sync([row(url=url)], tmp_path, make_fetcher(session))
        assert results[0].status == "error"
        assert session.calls == []


class TestFetcher:
    def test_retries_503_with_backoff_then_succeeds(self):
        session = FakeSession({PAGE: [FakeHTTP(503), FakeHTTP(503), FakeHTTP(body=b"ok")]})
        fetcher = make_fetcher(session)
        download = fetcher.download(PAGE)
        assert download.outcome == "ok"
        assert download.body == b"ok"
        assert fetcher.sleeps == [2, 4]

    def test_gives_up_after_the_retry_budget(self):
        session = FakeSession({PAGE: [requests.ConnectionError("boom")]})
        download = make_fetcher(session, retries=2).download(PAGE)
        assert download.outcome == "error"
        assert "3 attempts" in download.detail
        assert session.calls.count(PAGE) == 3

    def test_other_4xx_is_not_retried(self):
        session = FakeSession({PAGE: [FakeHTTP(403)]})
        download = make_fetcher(session).download(PAGE)
        assert download.outcome == "error"
        assert session.calls.count(PAGE) == 1

    def test_declared_size_over_the_cap_is_refused(self):
        session = FakeSession({PAGE: [FakeHTTP(body=b"x", headers={"content-length": "999"})]})
        download = make_fetcher(session, max_bytes=100).download(PAGE)
        assert download.outcome == "error"
        assert "cap" in download.detail

    def test_streamed_body_over_the_cap_is_refused(self):
        session = FakeSession({PAGE: [FakeHTTP(body=b"x" * 200)]})
        download = make_fetcher(session, max_bytes=100).download(PAGE)
        assert download.outcome == "error"

    def test_requests_to_one_host_are_spaced_by_the_delay(self):
        # reads: set 1st stamp, measure elapsed for the 2nd request, set its stamp
        clock = iter([0.0, 0.5, 0.5])
        session = FakeSession({PAGE: [FakeHTTP(body=b"a")], PDF: [FakeHTTP(body=b"b")]})
        sleeps: list[float] = []
        fetcher = fetch.Fetcher(session, delay=2.0, sleep=sleeps.append, clock=lambda: next(clock))
        fetcher.download(PAGE)
        fetcher.download(PDF)
        assert sleeps == [1.5]

    def test_responses_are_closed(self):
        response = FakeHTTP(body=b"a")
        make_fetcher(FakeSession({PAGE: [response]})).download(PAGE)
        assert response.closed


class TestManifest:
    def write(self, path, text):
        path.write_text(text, encoding="utf-8")

    def test_round_trip_preserves_columns_and_other_rows(self, tmp_path):
        manifest = tmp_path / "MANIFEST.csv"
        header = ",".join(fetch.MANIFEST_COLUMNS)
        self.write(manifest, f"{header}\na,,https://x/a.html,Title A,,MIT,,session,,\n")
        names, rows = fetch.read_manifest(manifest)
        rows[0]["sha256"] = "abc"
        fetch.write_manifest(manifest, names, rows)
        names2, rows2 = fetch.read_manifest(manifest)
        assert names2 == names
        assert rows2[0]["title"] == "Title A"
        assert rows2[0]["sha256"] == "abc"
        assert "\r" not in manifest.read_text(encoding="utf-8")

    def test_missing_required_column_is_rejected(self, tmp_path):
        manifest = tmp_path / "MANIFEST.csv"
        self.write(manifest, "id,title\na,T\n")
        with pytest.raises(ValueError, match="source_url"):
            fetch.read_manifest(manifest)

    def test_duplicate_and_invalid_ids_are_rejected(self, tmp_path):
        manifest = tmp_path / "MANIFEST.csv"
        header = ",".join(fetch.MANIFEST_COLUMNS)
        self.write(manifest, f"{header}\na,,u,,,,,,,\na,,u,,,,,,,\n")
        with pytest.raises(ValueError, match="duplicate"):
            fetch.read_manifest(manifest)
        self.write(manifest, f"{header}\n../x,,u,,,,,,,\n")
        with pytest.raises(ValueError, match="invalid id"):
            fetch.read_manifest(manifest)

    def test_the_shipped_manifest_is_well_formed(self):
        # read_manifest also rejects bad and duplicate ids
        names, rows = fetch.read_manifest(fetch.REPO_CORPUS / "MANIFEST.csv")
        assert tuple(names) == fetch.MANIFEST_COLUMNS
        assert all(row["source_url"].startswith(("http://", "https://")) for row in rows)


class TestMain:
    def test_empty_manifest_is_a_clean_no_op(self, tmp_path, capsys):
        manifest = tmp_path / "MANIFEST.csv"
        manifest.write_text(",".join(fetch.MANIFEST_COLUMNS) + "\n", encoding="utf-8")
        assert fetch.main(["--manifest", str(manifest), "--raw-dir", str(tmp_path / "raw")]) == 0
        assert "no sources" in capsys.readouterr().out

    def test_unreadable_manifest_exits_2(self, tmp_path):
        assert fetch.main(["--manifest", str(tmp_path / "missing.csv")]) == 2

    def test_raw_dir_follows_the_environment_variable(self, tmp_path, monkeypatch):
        monkeypatch.setenv(fetch.CORPUS_ENV, str(tmp_path))
        assert fetch.default_raw_dir() == tmp_path / "raw"
        monkeypatch.delenv(fetch.CORPUS_ENV)
        assert fetch.default_raw_dir() == fetch.REPO_CORPUS / "raw"

    def test_end_to_end_updates_the_manifest_only_when_not_dry_run(
        self, tmp_path, monkeypatch, capsys
    ):
        manifest = tmp_path / "MANIFEST.csv"
        header = ",".join(fetch.MANIFEST_COLUMNS)
        manifest.write_text(f"{header}\nguide,,{PAGE},,,,,,,\n", encoding="utf-8")
        session = FakeSession({PAGE: [FakeHTTP(body=b"hello")]})
        monkeypatch.setattr(fetch.requests, "Session", lambda: _Closable(session))
        raw = tmp_path / "raw"
        args = ["--manifest", str(manifest), "--raw-dir", str(raw), "--delay", "0"]

        assert fetch.main([*args, "--dry-run"]) == 0
        assert "sha256" in manifest.read_text(encoding="utf-8").splitlines()[0]
        assert fetch.read_manifest(manifest)[1][0]["sha256"] == ""
        assert not raw.exists()

        assert fetch.main(args) == 0
        assert fetch.read_manifest(manifest)[1][0]["sha256"] == sha(b"hello")
        assert (raw / "guide.html").read_bytes() == b"hello"
        assert "new" in capsys.readouterr().out


class _Closable:
    """Wraps FakeSession with the close() that main() calls on a real Session."""

    def __init__(self, session) -> None:
        self._session = session

    def get(self, *args, **kwargs):
        return self._session.get(*args, **kwargs)

    def close(self) -> None:
        pass
