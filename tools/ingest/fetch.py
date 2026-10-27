"""Fetch the public source material listed in corpus/MANIFEST.csv.

The manifest is tracked in git; the downloaded files are not (see
corpus/README.md). Each run downloads every source, compares its SHA-256 with
the manifest and reports new / changed / unchanged / gone / blocked / error, so
a manifest diff doubles as a changelog of what upstream changed.

Downloaded content is untrusted data: it is stored and hashed, never executed
or interpreted here. Conversion happens in a later, separate step.

Politeness and safety: http(s) only, robots.txt honoured (unreachable or 5xx
counts as disallow, per RFC 9309), a delay between requests to one host,
bounded retries with backoff, a timeout, a response size cap, atomic writes,
and a manifest `path` that cannot escape the raw directory.

Where things live: the manifest is always the repo's corpus/MANIFEST.csv; raw
files go under $BIGFIX_CORPUS_DIR/raw when that is set (e.g. an approved
location outside the repo), else corpus/raw.

--dry-run downloads into memory and reports, but writes nothing to disk.

Stdout carries the report; diagnostics go to stderr via logging.
"""

import argparse
import csv
import hashlib
import logging
import os
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.robotparser
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

log = logging.getLogger("corpus.fetch")

REPO_CORPUS = Path(__file__).resolve().parents[2] / "corpus"
CORPUS_ENV = "BIGFIX_CORPUS_DIR"

MANIFEST_COLUMNS = (
    "id",
    "path",
    "source_url",
    "title",
    "retrieved_on",
    "license",
    "bigfix_version",
    "dialect",
    "sha256",
    "notes",
)
REQUIRED_COLUMNS = ("id", "source_url", "sha256")

USER_AGENT = "bigfix-root-mcp-corpus-fetch/0.1"
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_RETRIES = 3
MAX_BACKOFF_SECONDS = 30
CHUNK_BYTES = 64 * 1024

# outcomes that are also report statuses
STATUS_NEW = "new"
STATUS_CHANGED = "changed"
STATUS_UNCHANGED = "unchanged"
STATUS_GONE = "gone"
STATUS_BLOCKED = "blocked"
STATUS_ERROR = "error"
_OUTCOME_OK = "ok"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,8}")
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass
class Download:
    """Result of one download attempt."""

    outcome: str
    body: bytes = b""
    detail: str = ""
    content_type: str = ""
    # the URL actually served, after redirects; callers that care about scope
    # (the crawler) must check it, because a redirect can leave the site
    final_url: str = ""


@dataclass
class Result:
    """One manifest row's line in the report."""

    source_id: str
    status: str
    detail: str = ""


def corpus_base() -> Path:
    """Where untracked corpus data lives: $BIGFIX_CORPUS_DIR when set, else corpus/."""
    base = os.environ.get(CORPUS_ENV)
    return Path(base) if base else REPO_CORPUS


def default_raw_dir() -> Path:
    """Raw directory: $BIGFIX_CORPUS_DIR/raw when set, else corpus/raw."""
    return corpus_base() / "raw"


def read_manifest(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    """Read the manifest, returning (fieldnames, rows). Raises ValueError if malformed."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        missing = [name for name in REQUIRED_COLUMNS if name not in fieldnames]
        if missing:
            raise ValueError(f"{path}: missing required column(s): {', '.join(missing)}")
        rows = [{name: (row.get(name) or "") for name in fieldnames} for row in reader]
    seen: set[str] = set()
    for row in rows:
        source_id = row["id"].strip()
        if not _ID_RE.match(source_id):
            raise ValueError(f"{path}: invalid id {source_id!r} (use letters, digits, . _ -)")
        if source_id in seen:
            raise ValueError(f"{path}: duplicate id {source_id!r}")
        seen.add(source_id)
    return fieldnames, rows


def write_manifest(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, str]]) -> None:
    """Rewrite the manifest atomically, keeping column order and LF line endings."""
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly before the replace
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with tmp:
            writer = csv.DictWriter(tmp, fieldnames=list(fieldnames), lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp.name, path)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def raw_path_for(row: dict[str, str], raw_dir: Path) -> tuple[str, Path]:
    """Return (path relative to raw_dir, absolute target) for one manifest row.

    Raises ValueError when the row's path would land outside raw_dir.
    """
    relative = row.get("path", "").strip()
    if not relative:
        suffix = Path(urllib.parse.urlsplit(row["source_url"]).path).suffix
        if not _SUFFIX_RE.fullmatch(suffix):
            suffix = ".bin"
        relative = row["id"].strip() + suffix
    root = raw_dir.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError(f"path {relative!r} escapes the raw directory")
    return relative, target


def atomic_write(target: Path, body: bytes) -> None:
    """Write body to target via a temp file and rename, creating parent directories."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly before the replace
        "wb", dir=target.parent, delete=False, suffix=".tmp"
    )
    try:
        with tmp:
            tmp.write(body)
        os.replace(tmp.name, target)
    except BaseException:
        Path(tmp.name).unlink(missing_ok=True)
        raise


class RobotsCache:
    """robots.txt per origin, fetched once per run."""

    def __init__(self, session, user_agent: str, timeout: float) -> None:
        self._session = session
        self._user_agent = user_agent
        self._timeout = timeout
        self._parsers: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._parsers:
            self._parsers[origin] = self._load(origin)
        parser = self._parsers[origin]
        return True if parser is None else parser.can_fetch(self._user_agent, url)

    def _load(self, origin: str) -> urllib.robotparser.RobotFileParser | None:
        """None means "no restrictions"; a deny-all parser means "do not fetch"."""
        deny_all = urllib.robotparser.RobotFileParser()
        deny_all.parse(["User-agent: *", "Disallow: /"])
        try:
            response = self._session.get(
                f"{origin}/robots.txt",
                headers={"User-Agent": self._user_agent},
                timeout=self._timeout,
                stream=False,
            )
        except requests.RequestException as err:
            log.warning("robots.txt for %s unreachable (%s); treating as disallow", origin, err)
            return deny_all
        try:
            status = response.status_code
            if status >= 500:
                log.warning(
                    "robots.txt for %s returned HTTP %s; treating as disallow", origin, status
                )
                return deny_all
            if status >= 400:
                return None
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(response.text.splitlines())
            return parser
        finally:
            response.close()


class Fetcher:
    """Downloads URLs politely: robots, per-host delay, retries, size cap."""

    def __init__(
        self,
        session,
        *,
        delay: float = DEFAULT_DELAY_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        retries: int = DEFAULT_RETRIES,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._delay = delay
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._retries = retries
        self._sleep = sleep
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._robots = RobotsCache(session, USER_AGENT, timeout)

    def download(self, url: str) -> Download:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return Download(
                STATUS_ERROR, detail=f"unsupported URL {url!r}: only http(s) is fetched"
            )
        if not self._robots.allowed(url):
            return Download(STATUS_BLOCKED, detail="disallowed by robots.txt")
        last_problem = ""
        for attempt in range(self._retries + 1):
            if attempt:
                self._sleep(min(2**attempt, MAX_BACKOFF_SECONDS))
            self._throttle(parts.netloc)
            try:
                response = self._session.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=self._timeout,
                    stream=True,
                )
            except requests.RequestException as err:
                last_problem = f"{type(err).__name__}: {err}"
                continue
            try:
                status = response.status_code
                if status in (404, 410):
                    return Download(STATUS_GONE, detail=f"HTTP {status}")
                if status in _RETRY_STATUSES:
                    last_problem = f"HTTP {status}"
                    continue
                if status >= 400:
                    return Download(STATUS_ERROR, detail=f"HTTP {status}")
                return self._read_body(response, url)
            finally:
                response.close()
        return Download(
            STATUS_ERROR, detail=f"gave up after {self._retries + 1} attempts: {last_problem}"
        )

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            wait = self._delay - (self._clock() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = self._clock()

    def _read_body(self, response, requested_url: str) -> Download:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            return Download(STATUS_ERROR, detail=f"declared size {declared} exceeds the cap")
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(CHUNK_BYTES):
                total += len(chunk)
                if total > self._max_bytes:
                    return Download(
                        STATUS_ERROR, detail=f"response exceeds the {self._max_bytes}-byte cap"
                    )
                chunks.append(chunk)
        except requests.RequestException as err:
            return Download(STATUS_ERROR, detail=f"{type(err).__name__} while reading: {err}")
        return Download(
            _OUTCOME_OK,
            body=b"".join(chunks),
            content_type=response.headers.get("content-type", ""),
            final_url=getattr(response, "url", "") or requested_url,
        )


def record_download(
    row: dict[str, str],
    download: Download,
    raw_dir: Path,
    *,
    dry_run: bool = False,
    today: date | None = None,
) -> Result:
    """Compare a successful download with the manifest row; store it unless dry_run.

    Idempotent: an unchanged source leaves the row alone, and only restores the
    local file if it is missing (a fresh checkout). Updates the row in place.
    """
    source_id = row["id"].strip()
    try:
        relative, target = raw_path_for(row, raw_dir)
    except ValueError as err:
        return Result(source_id, STATUS_ERROR, str(err))
    digest = hashlib.sha256(download.body).hexdigest()
    known = row.get("sha256", "").strip().lower()
    if digest == known:
        detail = ""
        if not dry_run and not target.exists():
            atomic_write(target, download.body)
            detail = "restored missing local copy"
        return Result(source_id, STATUS_UNCHANGED, detail)
    if not dry_run:
        atomic_write(target, download.body)
        row["path"] = relative
        row["sha256"] = digest
        row["retrieved_on"] = (today or date.today()).isoformat()
    return Result(
        source_id,
        STATUS_CHANGED if known else STATUS_NEW,
        f"sha256 {digest[:12]}, {len(download.body)} bytes",
    )


def sync(
    rows: list[dict[str, str]],
    raw_dir: Path,
    fetcher: Fetcher,
    *,
    dry_run: bool = False,
    only: Collection[str] | None = None,
    today: date | None = None,
) -> list[Result]:
    """Fetch every selected row; update the rows in place unless dry_run."""
    results: list[Result] = []
    for row in rows:
        source_id = row["id"].strip()
        if only and source_id not in only:
            continue
        try:
            raw_path_for(row, raw_dir)  # refuse an unsafe path before any request
        except ValueError as err:
            results.append(Result(source_id, STATUS_ERROR, str(err)))
            continue
        download = fetcher.download(row["source_url"].strip())
        if download.outcome != _OUTCOME_OK:
            results.append(Result(source_id, download.outcome, download.detail))
            continue
        results.append(record_download(row, download, raw_dir, dry_run=dry_run, today=today))
    return results


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ingest.fetch",
        description="Fetch the sources listed in corpus/MANIFEST.csv into the raw directory.",
    )
    parser.add_argument("--dry-run", action="store_true", help="download and report, write nothing")
    parser.add_argument("--only", action="append", metavar="ID", help="limit to a source id")
    parser.add_argument("--manifest", type=Path, default=REPO_CORPUS / "MANIFEST.csv")
    parser.add_argument("--raw-dir", type=Path, help=f"default: ${CORPUS_ENV}/raw, else corpus/raw")
    parser.add_argument(
        "--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="seconds per host"
    )
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. Exit 0 ok, 1 if any source errored, 2 if the manifest is unusable."""
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    try:
        fieldnames, rows = read_manifest(args.manifest)
    except (OSError, ValueError) as err:
        # a bad manifest is a user error; a traceback would only bury the message
        log.error("cannot read manifest: %s", err)  # noqa: TRY400
        return 2

    raw_dir = args.raw_dir or default_raw_dir()
    session = requests.Session()
    fetcher = Fetcher(
        session,
        delay=args.delay,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
        retries=args.retries,
    )
    try:
        results = sync(rows, raw_dir, fetcher, dry_run=args.dry_run, only=args.only)
    finally:
        session.close()

    for result in results:
        sys.stdout.write(f"{result.status:<10} {result.source_id}  {result.detail}".rstrip() + "\n")
    if not results:
        sys.stdout.write("no sources to fetch (is corpus/MANIFEST.csv empty?)\n")

    changed = any(result.status in (STATUS_NEW, STATUS_CHANGED) for result in results)
    if changed and not args.dry_run:
        write_manifest(args.manifest, fieldnames, rows)
        sys.stdout.write(f"updated {args.manifest}\n")
    return 1 if any(result.status == STATUS_ERROR for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
