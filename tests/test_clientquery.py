"""Unit tests for the client fast query helpers."""

import json

import pytest

from bigfix_root_mcp import clientquery
from tests.conftest import FakeBESConnection, FakeRESTResult


def make_results_envelope(rows):
    # the real server returns exactly one key, "results" - no count, no
    # completion flag (see docs/client-query.md)
    return {"results": rows}


def queue_results(conn, row_sets):
    for rows in row_sets:
        conn.get_responses.append(FakeRESTResult(text=json.dumps(make_results_envelope(rows))))


SUBMIT_RESPONSE_XML = (
    '<BESAPI xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
    '<ClientQuery Resource="https://bes.example.com:52311/api/clientquery/42">'
    "<ID>42</ID></ClientQuery></BESAPI>"
)


class TestBuildTargetXml:
    def test_all_computers_uses_custom_relevance_not_allcomputers(self):
        """A BigFix 11 root server rejects <AllComputers> outright:

            400 XML parsing error: no declaration found for element
            'AllComputers'

        besapi's get_target_xml emits it and docs describe it as valid, but it
        does not work. Client relevance TRUE is applicable on every agent and
        is accepted. Verified live; see docs/rest-endpoints.md.
        """
        xml_out, count = clientquery.build_target_xml(target_all=True)
        assert "AllComputers" not in xml_out
        assert xml_out == "<CustomRelevance>TRUE</CustomRelevance>"
        assert count is None

    def test_computer_ids(self):
        xml_out, count = clientquery.build_target_xml(computer_ids=[1, 2, 3])
        assert xml_out == (
            "<ComputerID>1</ComputerID><ComputerID>2</ComputerID><ComputerID>3</ComputerID>"
        )
        assert count == 3

    def test_computer_names_escaped(self):
        xml_out, count = clientquery.build_target_xml(computer_names=["host<1>", "a&b"])
        assert "<ComputerName>host&lt;1&gt;</ComputerName>" in xml_out
        assert "<ComputerName>a&amp;b</ComputerName>" in xml_out
        assert count == 2

    def test_relevance_escaped(self):
        xml_out, count = clientquery.build_target_xml(
            target_relevance='name of operating system contains "Win" AND 1 < 2'
        )
        assert "&lt;" in xml_out
        assert xml_out.startswith("<CustomRelevance>")
        assert count is None

    def test_no_target_rejected(self):
        with pytest.raises(ValueError, match="Exactly one targeting mode"):
            clientquery.build_target_xml()

    def test_two_targets_rejected(self):
        with pytest.raises(ValueError, match="Exactly one targeting mode"):
            clientquery.build_target_xml(target_all=True, computer_ids=[1])


class TestBuildClientQueryXml:
    def test_query_text_escaped(self):
        payload = clientquery.build_client_query_xml(
            'exists files whose (name of it contains "]]>") of folder "c:\\" whose (1 < 2 & true)',
            "<AllComputers>true</AllComputers>",
        )
        assert "]]>" not in payload.split("<QueryText>")[1].split("</QueryText>")[0]
        assert "&lt;" in payload
        assert "&amp;" in payload
        assert payload.startswith("<BESAPI><ClientQuery>")
        assert "<ApplicabilityRelevance>true</ApplicabilityRelevance>" in payload

    def test_empty_query_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            clientquery.build_client_query_xml("  ", "<AllComputers>true</AllComputers>")

    def test_client_context_is_left_out_by_default(self):
        payload = clientquery.build_client_query_xml("computer name", "<x/>")
        assert "UseClientContext" not in payload

    def test_client_context_goes_between_query_text_and_target(self):
        """The schema's sequence: ApplicabilityRelevance, QueryText, UseClientContext, Target."""
        payload = clientquery.build_client_query_xml(
            "version of registration server", "<x/>", use_client_context=True
        )
        assert (
            "<QueryText>version of registration server</QueryText>"
            "<UseClientContext>true</UseClientContext><Target><x/></Target>"
        ) in payload


class TestSubmitClientQuery:
    def test_returns_int_id(self):
        conn = FakeBESConnection()
        conn.post_responses.append(FakeRESTResult(text=SUBMIT_RESPONSE_XML))
        query_id = clientquery.submit_client_query(
            conn, "computer name", "<AllComputers>true</AllComputers>"
        )
        assert query_id == 42
        assert isinstance(query_id, int)
        method, path, data, _ = conn.calls[0]
        assert method == "post"
        assert path.endswith("/api/clientquery")
        assert "<QueryText>computer name</QueryText>" in data

    def test_missing_id_raises(self):
        conn = FakeBESConnection()
        conn.post_responses.append(FakeRESTResult(text="<BESAPI></BESAPI>"))
        with pytest.raises(ValueError, match="did not return a client query ID"):
            clientquery.submit_client_query(
                conn, "computer name", "<AllComputers>true</AllComputers>"
            )

    def test_http_error_raises(self):
        import requests

        conn = FakeBESConnection()
        conn.post_responses.append(FakeRESTResult(text="bad", status_code=500))
        with pytest.raises(requests.exceptions.HTTPError):
            clientquery.submit_client_query(
                conn, "computer name", "<AllComputers>true</AllComputers>"
            )

    def test_prefers_native_besapi_method(self):
        conn = FakeBESConnection()
        conn.client_query_submit = lambda query_text, target_xml: "7"
        assert clientquery.submit_client_query(conn, "computer name", "<x/>") == 7
        assert conn.calls == []  # local implementation not used

    def test_client_context_bypasses_the_native_method_that_cannot_send_it(self):
        conn = FakeBESConnection()
        conn.client_query_submit = lambda query_text, target_xml: "7"
        conn.post_responses.append(FakeRESTResult(text=SUBMIT_RESPONSE_XML))
        query_id = clientquery.submit_client_query(
            conn, "computer name", "<x/>", use_client_context=True
        )
        assert query_id == 42
        assert "<UseClientContext>true</UseClientContext>" in conn.calls[0][2]


class TestSummarizeResults:
    def test_counts_distinct_computers(self):
        rows = [
            {"computerID": 1, "computerName": "a", "result": "x"},
            {"computerID": 1, "computerName": "a", "result": "y"},
            {"computerID": 2, "computerName": "b", "result": "z"},
        ]
        summary = clientquery.summarize_results(make_results_envelope(rows), 42)
        assert summary["query_id"] == 42
        assert summary["reported_count"] == 2
        assert summary["result_row_count"] == 3
        assert summary["results"] == rows

    def test_empty_results(self):
        summary = clientquery.summarize_results({"results": []}, 5)
        assert summary["reported_count"] == 0
        assert summary["result_row_count"] == 0


class TestPollClientQuery:
    async def test_stops_when_expected_count_reached(self):
        conn = FakeBESConnection()
        queue_results(
            conn,
            [
                [{"computerID": 1, "result": "a"}],
                [{"computerID": 1, "result": "a"}, {"computerID": 2, "result": "b"}],
            ],
        )
        summary = await clientquery.poll_client_query(
            conn,
            42,
            timeout_seconds=30,
            poll_interval_seconds=0,
            stable_polls=5,
            expected_count=2,
        )
        assert summary["stop_reason"] == "expected_count_reached"
        assert summary["reported_count"] == 2
        assert summary["polls"] == 2

    async def test_stops_when_results_stable(self):
        conn = FakeBESConnection()
        rows = [{"computerID": 1, "result": "a"}]
        queue_results(conn, [rows, rows, rows, rows])
        summary = await clientquery.poll_client_query(
            conn,
            42,
            timeout_seconds=30,
            poll_interval_seconds=0,
            stable_polls=2,
            expected_count=None,
        )
        assert summary["stop_reason"] == "results_stable"
        # poll 1 sets baseline; polls 2 and 3 are the two stable polls
        assert summary["polls"] == 3

    async def test_stops_on_timeout_with_partial_results(self):
        conn = FakeBESConnection()
        # never stabilizes: a new computer reports every poll
        queue_results(
            conn,
            [[{"computerID": n, "result": "x"} for n in range(i + 1)] for i in range(50)],
        )
        summary = await clientquery.poll_client_query(
            conn,
            42,
            timeout_seconds=0.05,
            poll_interval_seconds=0.01,
            stable_polls=99,
            expected_count=None,
        )
        assert summary["stop_reason"] == "timeout"
        assert summary["reported_count"] >= 1  # partial results returned, not an error

    async def test_progress_callback_invoked_each_poll(self):
        conn = FakeBESConnection()
        queue_results(
            conn,
            [
                [{"computerID": 1, "result": "a"}],
                [{"computerID": 1, "result": "a"}, {"computerID": 2, "result": "b"}],
            ],
        )
        progress_calls = []

        async def progress_cb(reported, expected, message):
            progress_calls.append((reported, expected, message))

        await clientquery.poll_client_query(
            conn,
            42,
            timeout_seconds=30,
            poll_interval_seconds=0,
            stable_polls=5,
            expected_count=2,
            progress_cb=progress_cb,
        )
        assert [(r, e) for r, e, _ in progress_calls] == [(1, 2), (2, 2)]
        assert "1/2 computers reported" in progress_calls[0][2]

    async def test_zero_results_does_not_count_as_stable(self):
        conn = FakeBESConnection()
        queue_results(conn, [[], [], [], [{"computerID": 1, "result": "a"}]])
        summary = await clientquery.poll_client_query(
            conn,
            42,
            timeout_seconds=30,
            poll_interval_seconds=0,
            stable_polls=2,
            expected_count=1,
        )
        assert summary["stop_reason"] == "expected_count_reached"
        assert summary["polls"] == 4
