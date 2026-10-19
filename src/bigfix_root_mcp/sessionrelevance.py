"""Session relevance evaluation via bigfix-remote-session-relevance.

besapi is still the client for every other REST call; only /api/query moves
here. besapi's session_relevance_json always form-posts a double-quoted
query, which is right for a root server with no reachable BigFix Explorer
but breaks queries on one that forwards to Explorer. The library's REST
backend sends the JSON body first and falls back to the form on the
"no reachable BigFix Explorer" 503, so both setups evaluate exactly.

Connection settings come from connection.load_config() - the same env vars
and besapi.conf files besapi uses - read once and handed to the library
explicitly, never through its own ~/.bigfix config, and deployment-id
discovery is off. The timeouts are besapi's.

TLS: with BES_SSL_VERIFY unset (or "tofu") session relevance uses the
library's trust on first use. A CA-valid cert is used as is; a self-signed
one - the BigFix default - is pinned on first contact in the library's
~/.bigfix/.remote_session_known_certs.toml (shared with its CLI), and from
then on a different cert is refused before any credentials are sent. An
explicit false, true or CA path means what it means for besapi (true also
honours requests' CA-bundle env vars), and pins nothing. besapi itself has
no pinning and keeps verify=False in the unset/tofu case. One more
difference: the library's httpx transport ignores HTTPS_PROXY / NO_PROXY.

A successful answer is returned as the server's own JSON, not rebuilt from
the library's typed answers: those coerce every type but integer and boolean
to a string, so a floating point answer would come back as "1.5".

The library's own SessionRelevanceClient is not used: its anyio blocking
portal deadlocks at interpreter exit unless close() is called first, and
this server keeps its client for the life of the process. Instead the async
client lives on an event loop in a daemon thread, which exit simply drops.

This module must not import fastmcp (same layering rule as clientquery.py):
it returns the /api/query JSON envelope, which errors.check_relevance_envelope
already understands, and raises plain exceptions that errors.bes_errors
translates.
"""

import asyncio
import os
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from bigfix_remote_session_relevance import (
    AsyncSessionRelevanceClient,
    Config,
    ConfigError,
    ConnectionConfig,
    SessionRelevanceResult,
    TrustStore,
)
from bigfix_remote_session_relevance.trust import default_known_certs_path

from bigfix_root_mcp import connection

# the name the library reports this connection under (logs, result.connection)
CONNECTION_NAME = "bigfix-root-mcp"

# besapi's DEFAULT_TIMEOUT: heavy session relevance can run for minutes, and
# the library's own defaults (5 s / 120 s) would cut those queries off
CONNECT_TIMEOUT_SECONDS = 90
READ_TIMEOUT_SECONDS = 600

# requests honours these with verify=True, so besapi does; httpx does not
_REQUESTS_CA_ENV = ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

# TLS failures about a pinned (or unpinnable) cert, which need the pin file
_PIN_CODES = ("pin_mismatch", "pinned_cert_invalid", "untrusted")

# server text kept in an error message, like errors._snippet
_DETAIL_LEN = 500

# replaced in tests with one that returns an httpx.MockTransport
_transport_factory = None

T = TypeVar("T")

_lock = threading.Lock()
# serializes building/replacing the client so two threads never both build
# one; never taken on the loop thread, so holding it across _run() is safe
_build_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_client: AsyncSessionRelevanceClient | None = None


class SessionRelevanceUnavailableError(RuntimeError):
    """The query never got an answer: unreachable, rejected login, TLS, HTTP."""


def _verify(verify: bool | str) -> bool | str:
    """BES_SSL_VERIFY as requests (and so besapi) would apply it."""
    if verify is True:
        for name in _REQUESTS_CA_ENV:
            bundle = os.environ.get(name, "").strip()
            if bundle:
                verify = bundle
                break
    if isinstance(verify, str) and not os.path.isfile(verify):
        # otherwise the TLS setup fails later with a bare FileNotFoundError
        raise connection.ConnectionConfigError(f"CA bundle not found: {verify}")
    return verify


def _connection_config(config: connection.BESConfig) -> ConnectionConfig:
    return ConnectionConfig(
        name=CONNECTION_NAME,
        backend="rest",
        server=config.rootserver,
        username=config.username,
        password=config.password,
        # "tofu" pins through the TrustStore that _make_client passes
        verify="tofu" if config.tofu else _verify(config.verify),
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        read_timeout=READ_TIMEOUT_SECONDS,
    )


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine on the background loop and wait for it, from any thread.

    The tools are sync and may run concurrently on worker threads; they all
    share one loop, so the client's connection pool and locks live in one
    place.
    """
    global _loop  # noqa: PLW0603 - started once, on first use
    with _lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(
                target=_loop.run_forever, name="bigfix-root-mcp-session-relevance", daemon=True
            ).start()
        loop = _loop
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


async def _make_client(config: connection.BESConfig) -> AsyncSessionRelevanceClient:
    # built on the loop it will run on
    return AsyncSessionRelevanceClient(
        Config(connections=(_connection_config(config),)),
        # no callback: a first-seen self-signed cert is pinned and logged
        # (there is nobody to prompt behind an MCP server)
        trust=TrustStore() if config.tofu else None,
        discover=False,
        record_deployment_ids=False,
        transport_factory=_transport_factory,
    )


def _get_client() -> AsyncSessionRelevanceClient:
    """Lazy cached client, the counterpart of connection.get_connection().

    The config is read once, when the client is built, exactly like besapi's
    cached connection; connection.reset_connection() drops both together, so
    session relevance and the besapi tools never point at different servers.
    """
    global _client  # noqa: PLW0603 - lazy cache, cleared by reset()
    with _build_lock:
        if _client is None:
            _client = _run(_make_client(connection.load_config()))
        return _client


def reset() -> None:
    """Close and drop the cached client (called by connection.reset_connection)."""
    global _client
    with _build_lock:
        stale, _client = _client, None
        if stale is not None:
            _run(stale.aclose())


def _success_envelope(result: SessionRelevanceResult) -> dict:
    """The /api/query JSON envelope, as the server sent it when it sent JSON."""
    if result.raw is not None and result.raw.format == "json":
        data = result.raw_json()
        if isinstance(data, dict) and "result" in data:
            return data
    # BESAPI XML (an older server, or a proxy): only the typed answers exist
    envelope: dict = {"result": result.to_dict()["answers"]}
    if result.plural is not None:
        envelope["plural"] = result.plural
    if result.type is not None:
        envelope["type"] = result.type
    if result.evaluation_time_ms is not None:
        envelope["evaltime_ms"] = result.evaluation_time_ms
    return envelope


def _envelope(result: SessionRelevanceResult) -> dict:
    """Map a result to an envelope, or raise for anything but a relevance error."""
    error = result.error
    if error is None:
        return _success_envelope(result)
    if error.kind == "session_relevance":
        return {"error": error.message}
    if error.http_status == 403:
        raise PermissionError(f"HTTP 403: {error.message}")
    if error.kind == "connection":
        raise SessionRelevanceUnavailableError(
            f"cannot reach the BigFix root server: {error.message}"
        )
    if error.kind == "timeout":
        # slow is not down: say which limit was hit rather than "cannot reach"
        raise SessionRelevanceUnavailableError(
            f"the BigFix root server did not answer within {READ_TIMEOUT_SECONDS} s "
            f"(or connect within {CONNECT_TIMEOUT_SECONDS} s): {error.message}"
        )
    status = f" (HTTP {error.http_status})" if error.http_status else ""
    message = f"BigFix {error.kind} error{status}: {error.message}"
    if error.code in _PIN_CODES:
        message = (
            f"{message}. Pinned certificates: {default_known_certs_path()} - once the "
            "new certificate is verified out of band, remove its entry or run "
            "`bigfix-remote-session-relevance --forget-cert HOST:PORT`."
        )
    detail = (error.detail or "").strip()
    if detail and detail not in message:
        # the library's message is generic for http/format errors; the
        # server's own text is what says what went wrong
        if len(detail) > _DETAIL_LEN:
            detail = detail[:_DETAIL_LEN] + "..."
        message = f"{message}. Response: {detail}"
    raise SessionRelevanceUnavailableError(message)


def evaluate(session_relevance: str) -> dict:
    """Evaluate session relevance on the configured root server.

    Returns the server's {"result": [...], "plural": ..., "type": ...,
    "evaltime_ms": ...} on success or {"error": "..."} for a session
    relevance error, exactly the envelopes /api/query answers with. Anything
    that kept the query from being evaluated raises.
    """
    try:
        # transport failures come back as results; only config mistakes raise
        results = _run(_get_client().evaluate(session_relevance))
    except ConfigError as err:
        raise connection.ConnectionConfigError(
            f"unusable BigFix connection settings (check BES_ROOT_SERVER): {err}"
        ) from err
    return _envelope(results[0])
