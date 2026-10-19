"""Session relevance through bigfix-remote-session-relevance's REST backend.

The real library runs here, wire format and all; only the network is
replaced, by an httpx.MockTransport that answers like a root server.
"""

import json

import httpx
import pytest

from bigfix_root_mcp import connection, sessionrelevance


@pytest.fixture
def lab_config(monkeypatch):
    monkeypatch.setenv("BES_ROOT_SERVER", "https://bes.example.com:52311")
    monkeypatch.setenv("BES_USER_NAME", "testoperator")
    monkeypatch.setenv("BES_PASSWORD", "<PASSWORD>")
    monkeypatch.setenv("BES_SSL_VERIFY", "false")
    sessionrelevance.reset()
    yield
    sessionrelevance.reset()


@pytest.fixture
def server(monkeypatch, lab_config):
    """Install a scripted root server; returns (requests seen, responses to send)."""
    seen: list[httpx.Request] = []
    replies: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(
        sessionrelevance, "_transport_factory", lambda _conn: httpx.MockTransport(handler)
    )
    return seen, replies


def json_reply(payload, status=200):
    return httpx.Response(status, json=payload)


def test_sends_session_relevance_as_json_to_api_query(server):
    seen, replies = server
    replies.append(json_reply({"result": [3], "plural": False, "type": "integer"}))
    sessionrelevance.evaluate("number of bes computers")
    request = seen[0]
    assert request.url == "https://bes.example.com:52311/api/query"
    assert request.headers["content-type"] == "application/json+relevance"
    assert json.loads(request.content) == {"relevance": "number of bes computers"}
    # basic auth from the same config besapi uses
    assert request.headers["authorization"].startswith("Basic ")


def test_returns_the_rest_envelope_shape(server):
    _, replies = server
    replies.append(
        json_reply(
            {
                "result": [["host1", 1], ["host2", 2]],
                "plural": True,
                "type": "( string, integer )",
                "evaltime_ms": 7,
            }
        )
    )
    envelope = sessionrelevance.evaluate("(name of it, id of it) of bes computers")
    assert envelope == {
        "result": [["host1", 1], ["host2", 2]],
        "plural": True,
        "type": "( string, integer )",
        "evaltime_ms": 7,
    }


def test_session_relevance_error_becomes_error_envelope(server):
    _, replies = server
    replies.append(json_reply({"result": [], "error": 'The operator "firsts" is not defined.'}))
    envelope = sessionrelevance.evaluate("firsts 10 of bes computers")
    assert envelope == {"error": 'The operator "firsts" is not defined.'}


def test_falls_back_to_form_post_without_explorer(server):
    """Root server with no reachable Explorer: JSON gets a 503, form works."""
    seen, replies = server
    replies.append(httpx.Response(503, text="no reachable BigFix Explorer"))
    replies.append(json_reply({"result": ["a"], "plural": True, "type": "string"}))
    assert sessionrelevance.evaluate("names of bes sites")["result"] == ["a"]
    assert seen[1].headers["content-type"] == "application/x-www-form-urlencoded"
    # percent-quoted, then form-encoded again: what a no-Explorer server decodes
    assert b"relevance=names%2520of%2520bes%2520sites" in seen[1].content


def test_unreachable_server_raises_cannot_reach(server):
    _, replies = server
    replies.append(httpx.ConnectError("connection refused"))
    with pytest.raises(RuntimeError, match="cannot reach the BigFix root server"):
        sessionrelevance.evaluate("number of bes computers")


def test_bad_credentials_raise_without_leaking_the_password(server):
    _, replies = server
    replies.append(httpx.Response(401, text="Login failed."))
    with pytest.raises(RuntimeError, match="401") as excinfo:
        sessionrelevance.evaluate("number of bes computers")
    assert "<PASSWORD>" not in str(excinfo.value)


def test_forbidden_maps_to_permission_error(server):
    _, replies = server
    replies.append(httpx.Response(403, text="Forbidden"))
    with pytest.raises(PermissionError):
        sessionrelevance.evaluate("names of bes users")


def test_missing_config_raises_config_error(monkeypatch, tmp_path):
    for name in ("BES_ROOT_SERVER", "BES_USER_NAME", "BES_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(connection, "CONFIG_PATHS", [str(tmp_path / "none.conf")])
    sessionrelevance.reset()
    with pytest.raises(connection.ConnectionConfigError):
        sessionrelevance.evaluate("number of bes computers")


def test_config_is_read_once_and_client_reused(server, monkeypatch):
    """Like besapi's cached connection: no config re-read (and INFO log) per query."""
    _, replies = server
    replies.extend([json_reply({"result": [1]}), json_reply({"result": [2]})])
    reads = []
    real_load = connection.load_config
    monkeypatch.setattr(connection, "load_config", lambda: reads.append(1) or real_load())
    sessionrelevance.evaluate("1")
    first = sessionrelevance._client
    sessionrelevance.evaluate("2")
    assert sessionrelevance._client is first
    assert len(reads) == 1


def test_reset_connection_also_resets_session_relevance(server, monkeypatch):
    """One reset drops both clients, so they never point at different servers."""
    _, replies = server
    replies.append(json_reply({"result": [1]}))
    sessionrelevance.evaluate("1")
    first = sessionrelevance._client
    closed = []
    real_close = first.close

    def spy():
        closed.append(True)
        real_close()

    monkeypatch.setattr(first, "close", spy)
    connection.reset_connection()
    assert closed == [True]
    assert sessionrelevance._client is None

    replies.append(json_reply({"result": [3]}))
    monkeypatch.setenv("BES_ROOT_SERVER", "https://other.example.com:52311")
    seen, _ = server
    sessionrelevance.evaluate("3")
    assert seen[-1].url.host == "other.example.com"


# 0.5.1 fixed this in the library (an unclosed SessionRelevanceClient no
# longer hangs exit); kept as the guard now that the server relies on it
_EXIT_SCRIPT = """
import httpx
from bigfix_root_mcp import sessionrelevance

sessionrelevance._transport_factory = lambda _conn: httpx.MockTransport(
    lambda request: httpx.Response(200, json={"result": [1]})
)
assert sessionrelevance.evaluate("1")["result"] == [1]
# deliberately no reset(): the MCP server never closes its client either
"""


def test_cached_client_never_blocks_interpreter_exit(lab_config):
    """Regression: an unclosed client deadlocked at shutdown (library < 0.5.1).

    The server keeps its client for the life of the process and never closes
    it, so exiting with one still open must not hang.
    """
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, "-c", _EXIT_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_evaluate_works_from_concurrent_threads(monkeypatch, lab_config):
    """Each caller must get the answer to its own query, not just some answer."""
    from concurrent.futures import ThreadPoolExecutor

    def echo(request: httpx.Request) -> httpx.Response:
        query = json.loads(request.content)["relevance"]
        return json_reply({"result": [query], "type": "string"})

    monkeypatch.setattr(
        sessionrelevance, "_transport_factory", lambda _c: httpx.MockTransport(echo)
    )
    queries = [f'"q{i}"' for i in range(16)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        envelopes = list(pool.map(sessionrelevance.evaluate, queries))
    assert [e["result"] for e in envelopes] == [[q] for q in queries]


def test_numbers_pass_through_as_the_server_sent_them(server):
    """Regression: the library coerces floats to strings; besapi passed JSON on."""
    _, replies = server
    replies.append(
        json_reply(
            {
                "result": [["a", 1.5, 2, True]],
                "count": 1,
                "plural": True,
                "type": "( string, floating point, integer, boolean )",
                "evaltime_ms": 0.25,
            }
        )
    )
    envelope = sessionrelevance.evaluate("x")
    assert envelope["result"] == [["a", 1.5, 2, True]]
    assert envelope["count"] == 1
    assert envelope["evaltime_ms"] == 0.25


def test_xml_answer_still_becomes_an_envelope(server):
    """An older server (or proxy) may answer BESAPI XML; the library parses it."""
    _, replies = server
    replies.append(
        httpx.Response(
            200,
            headers={"content-type": "application/xml"},
            text=(
                '<BESAPI><Query Resource="x"><Result><Answer type="integer">3</Answer>'
                "</Result><Evaluation><Time>1ms</Time><Plurality>Singular</Plurality>"
                "</Evaluation></Query></BESAPI>"
            ),
        )
    )
    assert sessionrelevance.evaluate("number of bes computers")["result"] == [3]


def test_timeouts_match_besapi():
    config = connection.BESConfig("https://bes.example.com:52311", "u", "<PASSWORD>")
    conn = sessionrelevance._connection_config(config)
    assert (conn.connect_timeout, conn.read_timeout) == (90, 600)


def test_slow_server_is_not_reported_unreachable(server):
    _, replies = server
    replies.append(httpx.ReadTimeout("read timed out"))
    with pytest.raises(RuntimeError) as excinfo:
        sessionrelevance.evaluate("number of bes computers")
    message = str(excinfo.value)
    assert "cannot reach" not in message
    assert "did not answer within 600" in message


def test_http_error_keeps_the_server_body(server):
    _, replies = server
    replies.append(httpx.Response(500, text="boom: database unavailable"))
    with pytest.raises(RuntimeError, match="boom: database unavailable"):
        sessionrelevance.evaluate("number of bes computers")


def test_permission_401_is_not_reported_as_403(server):
    _, replies = server
    replies.append(httpx.Response(401, json={"error": "Explorer login permission required"}))
    with pytest.raises(RuntimeError, match="HTTP 401") as excinfo:
        sessionrelevance.evaluate("number of bes computers")
    assert not isinstance(excinfo.value, PermissionError)


def test_unusable_server_address_is_a_config_error(monkeypatch, lab_config):
    monkeypatch.setenv("BES_ROOT_SERVER", "https://bes.example.com:52311/foo")
    with pytest.raises(connection.ConnectionConfigError, match="BES_ROOT_SERVER"):
        sessionrelevance.evaluate("number of bes computers")


def test_missing_ca_bundle_is_a_config_error_naming_the_path(monkeypatch, lab_config, tmp_path):
    missing = tmp_path / "nope.pem"
    monkeypatch.setenv("BES_SSL_VERIFY", str(missing))
    with pytest.raises(connection.ConnectionConfigError, match=r"nope\.pem"):
        sessionrelevance.evaluate("number of bes computers")


@pytest.mark.parametrize("variable", ["REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"])
def test_verify_true_honours_the_requests_ca_bundle(monkeypatch, tmp_path, variable):
    """besapi (requests) trusts these with verify=True, so session relevance must too."""
    bundle = tmp_path / "corp.pem"
    bundle.write_text("placeholder")
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    monkeypatch.setenv(variable, str(bundle))
    config = connection.BESConfig("https://bes.example.com:52311", "u", "<PASSWORD>", verify=True)
    assert sessionrelevance._connection_config(config).verify == str(bundle)


def test_verify_true_without_ca_env_stays_true(monkeypatch):
    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    config = connection.BESConfig("https://bes.example.com:52311", "u", "<PASSWORD>", verify=True)
    assert sessionrelevance._connection_config(config).verify is True


async def test_tool_output_uses_the_real_envelope(server):
    """End to end through the MCP tool with only the network faked."""
    from fastmcp import Client

    from bigfix_root_mcp import server as mcp_server

    _, replies = server
    replies.append(
        json_reply({"result": [1.5], "count": 1, "plural": False, "type": "floating point"})
    )
    async with Client(mcp_server.mcp) as client:
        result = await client.call_tool("session_relevance_query", {"relevance": "1.5"})
    assert result.data["result"] == [1.5]
    assert result.data["type"] == "floating point"
