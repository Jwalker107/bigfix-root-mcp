"""BigFix client "fast query" (agent query) helpers built on besapi.

besapi has no built-in clientquery support; this module implements the flow
from besapi's examples/client_query_from_string.py with fixes (XML escaping
of query text and computer names, int-coerced query ID, bounded polling).

Upstream candidates: everything except poll_client_query is generic BigFix
REST logic intended to migrate into besapi.besapi.BESConnection (conn ->
self); see docs/besapi-proposals.md. Call sites prefer a native besapi
method when one exists. No fastmcp imports are allowed in this module, and
errors are raised as ValueError / requests.HTTPError in besapi style.

Client query semantics: POST /api/clientquery targets a set of clients with
client relevance; results accumulate at /api/clientqueryresults/{id} as
clients report in (typically over seconds to minutes). There is no
completion flag, so polling needs termination heuristics and partial
results at timeout are a normal outcome, not an error.
"""

import asyncio
import json
import time
import xml.sax.saxutils


def _check_status(result):
    """Raise requests.HTTPError on non-2xx; besapi only raises on 403."""
    result.request.raise_for_status()
    return result


def build_target_xml(
    *,
    target_all: bool = False,
    computer_ids: list[int] | None = None,
    computer_names: list[str] | None = None,
    target_relevance: str | None = None,
) -> "tuple[str, int | None]":
    """Build ClientQuery Target XML from exactly one targeting mode.

    Returns (target_xml, expected_count) where expected_count is the number
    of targeted computers when knowable (ids/names), else None.

    Built here rather than via besapi.besapi.get_target_xml because that
    helper does not XML-escape computer names and its relevance CDATA does
    not handle "]]>".
    """
    modes = [
        bool(target_all),
        bool(computer_ids),
        bool(computer_names),
        bool(target_relevance),
    ]
    if sum(modes) != 1:
        raise ValueError(
            "Exactly one targeting mode must be provided: target_all, "
            "computer_ids, computer_names, or target_relevance."
        )

    if target_all:
        # NOT <AllComputers>true</AllComputers>: a BigFix 11 root server
        # rejects that element with "400 XML parsing error: no declaration
        # found for element 'AllComputers'", even though besapi's
        # get_target_xml emits it. Client relevance TRUE is applicable on
        # every agent and is accepted. Verified live - docs/rest-endpoints.md.
        return "<CustomRelevance>TRUE</CustomRelevance>", None

    if computer_ids:
        xml_out = "".join(f"<ComputerID>{int(cid)}</ComputerID>" for cid in computer_ids)
        return xml_out, len(computer_ids)

    if computer_names:
        xml_out = "".join(
            f"<ComputerName>{xml.sax.saxutils.escape(str(name))}</ComputerName>"
            for name in computer_names
        )
        return xml_out, len(computer_names)

    if target_relevance:
        return (
            f"<CustomRelevance>{xml.sax.saxutils.escape(target_relevance)}</CustomRelevance>",
            None,
        )

    # Unreachable: the exactly-one-mode guard above already rejected the
    # no-mode case. Spelled out rather than left as a fallthrough so the
    # function cannot silently return None if that guard is ever changed.
    raise ValueError("Exactly one targeting mode must be provided.")


def build_client_query_xml(
    query_text: str, target_xml: str, *, use_client_context: bool = False
) -> str:
    """Build the BESAPI ClientQuery XML payload.

    query_text is escaped with xml.sax.saxutils.escape rather than CDATA
    wrapping: always correct, no "]]>" edge case.

    use_client_context adds <UseClientContext>true</UseClientContext>, which
    makes the agent evaluate the query in agent (client) context, where
    inspectors such as `registration server` exist; without it they fail
    with "No inspector context.". The schema orders it after QueryText and
    before Target, and the element is left out when not wanted.
    """
    if not query_text or not query_text.strip():
        raise ValueError("query_text must be a non-empty client relevance string.")
    context = "<UseClientContext>true</UseClientContext>" if use_client_context else ""
    return (
        "<BESAPI>"
        "<ClientQuery>"
        "<ApplicabilityRelevance>true</ApplicabilityRelevance>"
        f"<QueryText>{xml.sax.saxutils.escape(query_text)}</QueryText>"
        f"{context}"
        f"<Target>{target_xml}</Target>"
        "</ClientQuery>"
        "</BESAPI>"
    )


def submit_client_query(
    conn, query_text: str, target_xml: str, *, use_client_context: bool = False
) -> int:
    """POST a client query; return its integer query ID."""
    native = getattr(conn, "client_query_submit", None)
    # a native besapi method takes no client context option, so it is used only without one
    if native is not None and not use_client_context:
        return int(native(query_text, target_xml))

    payload = build_client_query_xml(query_text, target_xml, use_client_context=use_client_context)
    result = _check_status(conn.post(conn.url("clientquery"), data=payload))
    try:
        return int(result.besobj.ClientQuery.ID)
    except (AttributeError, TypeError, ValueError) as err:
        raise ValueError(
            "BigFix did not return a client query ID. Response was: " + str(result.text)[:500]
        ) from err


def fetch_client_query_results(conn, query_id: int) -> dict:
    """GET current (cumulative) results for a client query ID as JSON."""
    native = getattr(conn, "client_query_results", None)
    if native is not None:
        return native(query_id)

    result = _check_status(conn.get(conn.url(f"clientqueryresults/{int(query_id)}?output=json")))
    return json.loads(result.text)


def summarize_results(envelope: dict, query_id: int) -> dict:
    """Summarize a clientqueryresults JSON envelope, rows passed through unchanged.

    reported_count counts distinct computers, since one computer can return
    multiple result rows (plural client relevance results).
    """
    rows = envelope.get("results", [])
    computer_keys = set()
    for row in rows:
        if isinstance(row, dict):
            # field name observed as "computerID"; fall back to whole-row identity
            computer_keys.add(row.get("computerID", json.dumps(row, sort_keys=True)))
        else:
            computer_keys.add(str(row))
    return {
        "query_id": int(query_id),
        "reported_count": len(computer_keys),
        "result_row_count": len(rows),
        "results": rows,
    }


async def poll_client_query(
    conn,
    query_id: int,
    *,
    timeout_seconds: float = 60,
    poll_interval_seconds: float = 5,
    stable_polls: int = 3,
    expected_count: int | None = None,
    progress_cb=None,
) -> dict:
    """Poll client query results until a termination condition is met.

    Termination conditions, checked in order after each fetch:
      1. expected_count distinct computers reported -> "expected_count_reached"
      2. reported_count unchanged for stable_polls consecutive polls, once at
         least one result exists -> "results_stable"
      3. elapsed >= timeout_seconds -> "timeout"

    Results are cumulative and never complete with certainty; partial
    results at timeout are a normal outcome, not an error.

    progress_cb, if given, is an async callable(reported, expected, message)
    invoked after every fetch. This function stays in the MCP package (not
    an upstream candidate) because besapi's API is synchronous.
    """
    started = time.monotonic()
    polls = 0
    previous_reported = -1
    stable_count = 0
    summary = summarize_results({"results": []}, query_id)
    stop_reason = "timeout"

    while True:
        envelope = fetch_client_query_results(conn, query_id)
        summary = summarize_results(envelope, query_id)
        polls += 1
        elapsed = time.monotonic() - started
        reported = summary["reported_count"]

        if progress_cb is not None:
            await progress_cb(
                reported,
                expected_count,
                f"{reported}/{expected_count if expected_count else '?'} "
                f"computers reported, {elapsed:.0f}s elapsed",
            )

        if expected_count is not None and reported >= expected_count:
            stop_reason = "expected_count_reached"
            break

        if reported > 0 and reported == previous_reported:
            stable_count += 1
            if stable_count >= stable_polls:
                stop_reason = "results_stable"
                break
        else:
            stable_count = 0
        previous_reported = reported

        if elapsed >= timeout_seconds:
            stop_reason = "timeout"
            break

        # don't overshoot the timeout by a full poll interval
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            stop_reason = "timeout"
            break
        await asyncio.sleep(min(poll_interval_seconds, remaining))

    summary.update(
        {
            "stop_reason": stop_reason,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "expected_count": expected_count,
            "polls": polls,
        }
    )
    return summary
