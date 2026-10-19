"""Shared test fakes: scriptable stand-ins for besapi's BESConnection."""

import lxml.objectify
import pytest

import bigfix_root_mcp.connection as connection_module
from bigfix_root_mcp import sessionrelevance


class FakeResponse:
    """Mimics the requests.Response held by besapi's RESTResult."""

    def __init__(self, status_code=200, text="", headers=None, url="") -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "application/xml"}
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.exceptions.HTTPError(
                f"{self.status_code} error for url: {self.url}", response=self
            )


class FakeRESTResult:
    """Mimics besapi.besapi.RESTResult closely enough for the wrapper."""

    def __init__(self, text="", status_code=200, headers=None, url="") -> None:
        self.text = text
        self.request = FakeResponse(status_code=status_code, text=text, headers=headers, url=url)

    @property
    def besobj(self):
        return lxml.objectify.fromstring(self.text.encode())

    @property
    def besdict(self):
        # simplified stand-in; real besapi strips namespaces etc.
        return {"text": self.text}

    def __str__(self):
        return self.text


class FakeBESConnection:
    """Scriptable BESConnection: queues responses, records calls."""

    rootserver = "https://bes.example.com:52311"
    rootserver_port = 52311
    username = "testoperator"

    def __init__(self) -> None:
        self.calls = []
        # map of (method, path-prefix) is overkill; simple FIFO queues per verb
        self.get_responses = []
        self.post_responses = []
        self.relevance_responses = []
        self.is_main_operator = True

    def url(self, path):
        if str(path).startswith(self.rootserver):
            return path
        return f"{self.rootserver}/api/{path}"

    def get(self, path="help", **kwargs):
        self.calls.append(("get", path, kwargs))
        if not self.get_responses:
            raise AssertionError(f"unexpected GET {path}")
        return self.get_responses.pop(0)

    def post(self, path, data, **kwargs):
        self.calls.append(("post", path, data, kwargs))
        if not self.post_responses:
            raise AssertionError(f"unexpected POST {path}")
        return self.post_responses.pop(0)

    def get_user(self, user_name):
        self.calls.append(("get_user", user_name))
        if not self.get_responses:
            return None
        return self.get_responses.pop(0)

    def get_dashboard_variable_value(self, dashboard_name, var_name):
        self.calls.append(("get_dashboard_variable_value", dashboard_name, var_name))
        return "fake-value"

    def am_i_main_operator(self):
        return self.is_main_operator


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path_factory):
    """Every test gets its own HOME, so nothing can touch the real ~/.bigfix
    (session relevance pins self-signed certs there on first use)."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home


@pytest.fixture
def fake_conn(monkeypatch):
    """Install a FakeBESConnection as the cached module-level connection."""
    conn = FakeBESConnection()
    monkeypatch.setattr(connection_module, "_conn", conn)

    # session relevance goes through sessionrelevance, not besapi (which has
    # no session_relevance_json here, so a stray besapi query fails loudly);
    # relevance_responses holds /api/query envelopes or exceptions to raise
    def evaluate(session_relevance):
        conn.calls.append(("session_relevance", session_relevance))
        if not conn.relevance_responses:
            raise AssertionError("unexpected session relevance query")
        response = conn.relevance_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(sessionrelevance, "evaluate", evaluate)
    yield conn
    connection_module.reset_connection()
