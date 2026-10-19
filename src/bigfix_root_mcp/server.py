"""FastMCP server exposing read-only BigFix root server tools via besapi.

Only read-only tools are registered; no mutating besapi calls exist in this
package (registration is the guard). One documented nuance: submitting a
client query creates a query object server-side, but clients only evaluate
relevance against it, so it is operationally read-only.

Site path rule: this server never uses besapi's mutable "current site path"
connection state (set_current_site_path / get_current_site_path); tools
that need a site take an explicit site_path parameter.
"""

import importlib.resources
import json
import logging
import os
import sys
from typing import Annotated

import besapi.besapi
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.context import Context
from pydantic import Field

from bigfix_root_mcp import (
    __version__,
    actions,
    analysis,
    besxml,
    clientquery,
    connection,
    content,
    prompts,
    qna,
    response,
    sessionrelevance,
    writes,
)
from bigfix_root_mcp.errors import (
    bes_errors,
    check_relevance_envelope,
    check_rest_result,
)

logger = logging.getLogger(__name__)

# protect the root server from abusive polling parameters:
MAX_TIMEOUT_SECONDS = 600
MIN_POLL_INTERVAL_SECONDS = 2

# read once at import: the write tools below are registered only if this is on
WRITES_ENABLED = connection.writes_enabled()

# read once at import, like writes: the qna tools exist only when
# bigfix-remote-client-relevance is importable and a target source is allowed
# (see qna.qna_enabled)
QNA_ENABLED = qna.qna_enabled()

LIMIT_FIELD = Field(
    default=None,
    description=(
        f"Max rows to return (default {response.DEFAULT_LIMIT}). The response "
        "reports total_available and truncated."
    ),
)
OFFSET_FIELD = Field(default=0, description="Row offset, for paging through results.")


def _with_advisory(payload: dict, advisory: dict | None) -> dict:
    """Attach a static-analysis advisory, only when it has something to say.

    A clean query (the common case) adds zero tokens to the response.
    """
    if advisory:
        payload["analysis"] = advisory
    return payload


def _client_query_advisory(query_text: str, target_relevance: str | None) -> dict | None:
    """Preflight a client query's relevance and its targeting relevance.

    Targeting typos deserve the same advisory: bad target_relevance silently
    targets zero machines, which looks like 'no results' rather than an error.
    """
    advisory = analysis.preflight(query_text, dialect="client")
    if target_relevance:
        target_advisory = analysis.preflight(target_relevance, dialect="client")
        if target_advisory:
            advisory = dict(advisory) if advisory else {"note": target_advisory["note"]}
            advisory["target_relevance_findings"] = target_advisory["findings"]
    return advisory


def _bound_rows(payload: dict, key: str, limit, offset) -> dict:
    """Window the row list under `key`, merging the bounding metadata in.

    Counts already computed by the caller (reported_count, evaltime_ms...)
    describe the whole result set and are left untouched: they answer "how big
    is the answer", which stays true regardless of how much of it was returned.
    """
    bounded = response.bound_list(payload.get(key) or [], limit=limit, offset=offset)
    out = dict(payload)
    out[key] = bounded.pop("items")
    out.update(bounded)
    return out


mcp = FastMCP(
    name="bigfix-root-mcp",
    version=__version__,
    instructions=(
        "Read-only access to a HCL BigFix root server REST API. Use "
        "session_relevance_query for data already reported to the server "
        "(fast, no client round-trip). Use the client_query tools to ask "
        "live questions of BigFix agents; those results accumulate over "
        "seconds to minutes as clients report in.\n\n"
        "SCOPE: every result is limited to what the configured operator can "
        "see. Only a master operator has full visibility; a non-master "
        "operator can never be certain its view of BigFix is complete, and "
        "cannot distinguish 'does not exist' from 'outside my scope'. Check "
        "whoami (is_main_operator) before describing any result as the state "
        "of BigFix overall, and otherwise report results as visible to this "
        "operator."
    ),
)


# --------------------------------------------------------------------------
# Reference resources. Static markdown shipped in the package: relevance is
# the hard part for a model, and a cookbook it can pull on demand beats
# stuffing the same guidance into every tool description.
# --------------------------------------------------------------------------

_RESOURCES = {
    "bigfix://relevance/session-cookbook": (
        "session-cookbook.md",
        "Session relevance cookbook",
        (
            "Working session relevance expressions, verified against a live root "
            "server, plus the operators that do not exist."
        ),
    ),
    "bigfix://relevance/client-cookbook": (
        "client-cookbook.md",
        "Client relevance cookbook",
        "Client (fast query) relevance, targeting forms, and how to read cumulative results.",
    ),
    "bigfix://guide/tools": (
        "tool-guide.md",
        "Tool selection guide",
        (
            "Which tool answers which question, how to read bounded responses, "
            "and what operator scope means for any answer."
        ),
    ),
}


# Reference documents shipped inside bigfix-relevance-analyzer. Titles and
# summaries are hardcoded (matching _RESOURCES style) so registration stays
# import-free; the analyzer's `reference` module is deliberately not imported
# at its package root, and the lazy import below keeps that true here until a
# client actually reads one.
_ANALYZER_RESOURCES = {
    "bigfix://relevance/reference/dialects": (
        "dialects",
        "BigFix relevance: client versus session",
        "The two relevance dialects, where each is evaluated, and which one a tool expects.",
    ),
    "bigfix://relevance/reference/client-relevance": (
        "client-relevance",
        "BigFix client relevance: language reference",
        "Writing relevance evaluated on the endpoint: inspectors, types, and idioms.",
    ),
    "bigfix://relevance/reference/session-relevance": (
        "session-relevance",
        "BigFix session relevance: language reference",
        "Writing relevance evaluated on the server against the BigFix object model.",
    ),
}


def _read_resource_file(filename: str) -> str:
    return (
        importlib.resources.files(__package__)
        .joinpath("resources", filename)
        .read_text(encoding="utf-8")
    )


def _make_loader(filename: str):
    """Build a zero-argument loader.

    It has to take no parameters at all: FastMCP reads any function parameter
    as a URI template variable, and these URIs are static.
    """

    def _load() -> str:
        return _read_resource_file(filename)

    return _load


def _make_reference_loader(slug: str):
    """Zero-arg loader for one analyzer reference document (see _make_loader)."""

    def _load() -> str:
        from bigfix_relevance_analyzer import reference  # noqa: PLC0415 - lazy on purpose

        for document in reference.documents():
            if document.slug == slug:
                return document.read()
        raise ValueError(f"analyzer reference document {slug!r} not found")

    return _load


def _register_resources() -> None:
    """Register each markdown file as an MCP resource."""
    for uri, (filename, name, description) in _RESOURCES.items():
        mcp.resource(uri, name=name, description=description, mime_type="text/markdown")(
            _make_loader(filename)
        )
    for uri, (slug, name, description) in _ANALYZER_RESOURCES.items():
        mcp.resource(uri, name=name, description=description, mime_type="text/markdown")(
            _make_reference_loader(slug)
        )


_register_resources()
prompts.register_prompts(mcp)


TARGETING_DESCRIPTION = (
    "Targeting: set exactly one of target_all, target_computer_ids, "
    "target_computer_names, or target_relevance (client relevance evaluated "
    "on each agent to decide applicability). Targeting is limited to the "
    "configured operator's scope, so target_all means all computers this "
    "operator can see, not necessarily all computers in BigFix."
)


@mcp.tool
@bes_errors("session_relevance_query")
def session_relevance_query(
    relevance: Annotated[
        str,
        Field(description="A BigFix session relevance expression to evaluate."),
    ],
    limit: Annotated[int | None, LIMIT_FIELD] = None,
    offset: Annotated[int, OFFSET_FIELD] = 0,
) -> dict:
    """Evaluate a BigFix session relevance query on the root server.

    Session relevance queries data the server already has (computers,
    fixlets, actions, sites, operators...) with no client round-trip.
    Examples: 'number of bes computers', '(name of it, id of it) of bes
    computers whose (now - last report time of it < 1 * day)'.

    There is no way to limit rows in the relevance itself - 'first',
    'firsts', 'items' and 'elements' are all undefined operators on a BigFix
    root server. Use the limit/offset parameters instead; the response reports
    total_available and truncated. Note the server still evaluates the whole
    query, so limit bounds the response, not the work.

    Returns the server's JSON envelope: {"result": [...], "plural": ...,
    "type": ..., "evaltime_ms": ...} (plus "count" when the server sends
    it) and bounding metadata.

    Results are evaluated within the configured operator's scope. Unless
    whoami reports is_main_operator, counts and lists are a lower bound on
    what exists - report them as visible to this operator, not as the
    complete state of BigFix.
    """
    # advisory only: even a query the analyzer rejects is still sent - the
    # server is authoritative and the analyzer's tables are a snapshot
    advisory = analysis.preflight(relevance, dialect="session")
    envelope = sessionrelevance.evaluate(relevance)
    try:
        payload = check_relevance_envelope(envelope)
    except ToolError as err:
        appendix = analysis.error_appendix(advisory)
        if appendix:
            raise ToolError(f"{err}\n\nStatic analysis:\n{appendix}") from None
        raise
    return _with_advisory(_bound_rows(payload, "result", limit, offset), advisory)


@mcp.tool
@bes_errors("analyze_relevance")
def analyze_relevance(
    relevance: Annotated[
        str,
        Field(description="Relevance expression to analyze. It is never evaluated."),
    ],
    dialect: Annotated[
        str | None,
        Field(
            description=(
                "Pin the dialect: 'session' (root server) or 'client' (agent). "
                "Default: classify automatically from the text."
            )
        ),
    ] = None,
    platform: Annotated[
        str | None,
        Field(
            description=(
                "Narrow client inspector lookups to one platform, e.g. 'windows', "
                "'macos', 'ubuntu'."
            )
        ),
    ] = None,
    include_sexpr: Annotated[
        bool,
        Field(description="Include the parsed tree as an S-expression."),
    ] = False,
) -> dict:
    """Statically analyze a relevance expression WITHOUT evaluating it anywhere.

    Offline and instant: checks that the expression parses (with a positioned
    error when it does not), infers types, classifies the dialect (session vs
    client), flags inspectors no reference dump defines, finds unbound 'it',
    and scores complexity/evaluation cost. Use it to validate a query before
    session_relevance_query or client_query, or to understand a relevance
    error after one.

    Advisory: the inspector table is a snapshot, so an unknown inspector is a
    warning (the live server may still know it), and a clean report does not
    guarantee the server will accept the query.
    """
    return analysis.analyze(relevance, dialect, platform, include_sexpr)


@mcp.tool
@bes_errors("search_inspectors")
def search_inspectors(
    query: Annotated[
        str,
        Field(description="Inspector name or phrase, e.g. 'registry key', 'bes computers'."),
    ],
    dialect: Annotated[
        str | None,
        Field(description="'session' or 'client' to filter; default searches both."),
    ] = None,
    kind: Annotated[
        str | None,
        Field(
            description=(
                "Filter by inspector kind: 'property', 'cast', 'binary-operator' "
                "or 'unary-operator'."
            )
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(description=f"Max matches to return (1-{analysis.MAX_SEARCH_LIMIT})."),
    ] = 10,
) -> dict:
    """Search the BigFix inspector reference (offline snapshot; no server call).

    Fuzzy: returns exact, prefix and near-miss matches with signatures, result
    types and the dialect each inspector exists in. Use it before writing a
    query, or after an 'is not defined' error to find the right name. Snapshot
    caveat: absence here does not prove absence on the live server.
    """
    return analysis.search(query, dialect, kind, limit)


@mcp.tool(description=f"Submit a BigFix client (fast) query. {TARGETING_DESCRIPTION}")
@bes_errors("client_query_submit")
def client_query_submit(
    query_text: Annotated[
        str,
        Field(description="Client relevance to evaluate on each targeted agent."),
    ],
    target_all: Annotated[bool, Field(description="Target all computers.")] = False,
    target_computer_ids: Annotated[
        list[int] | None, Field(description="Target these BigFix computer IDs.")
    ] = None,
    target_computer_names: Annotated[
        list[str] | None, Field(description="Target these computer names.")
    ] = None,
    target_relevance: Annotated[
        str | None,
        Field(description="Client relevance targeting expression."),
    ] = None,
) -> dict:
    """Submit a client fast query and return its query_id immediately.

    Results accumulate over seconds to minutes as agents report in; fetch
    them (repeatedly) with client_query_results, or use the client_query
    tool to submit and wait in one call. expected_count is the number of
    targeted computers when knowable, else null.
    """
    # the core feedback-loop win: a typo'd inspector surfaces here, in the
    # submit response, instead of as per-computer errors a minute later
    advisory = _client_query_advisory(query_text, target_relevance)
    conn = connection.get_connection()
    target_xml, expected_count = clientquery.build_target_xml(
        target_all=target_all,
        computer_ids=target_computer_ids,
        computer_names=target_computer_names,
        target_relevance=target_relevance,
    )
    query_id = clientquery.submit_client_query(conn, query_text, target_xml)
    return _with_advisory({"query_id": query_id, "expected_count": expected_count}, advisory)


@mcp.tool
@bes_errors("client_query_results")
def client_query_results(
    query_id: Annotated[int, Field(description="Query ID returned by client_query_submit.")],
    limit: Annotated[int | None, LIMIT_FIELD] = None,
    offset: Annotated[int, OFFSET_FIELD] = 0,
) -> dict:
    """Fetch current results for a previously submitted client fast query.

    Results are cumulative and there is no completion flag: safe and cheap
    to call repeatedly until reported_count stops growing or the expected
    number of computers have reported.

    reported_count and result_row_count always describe the full result set;
    the returned rows are windowed by limit/offset.
    """
    conn = connection.get_connection()
    envelope = clientquery.fetch_client_query_results(conn, query_id)
    summary = clientquery.summarize_results(envelope, query_id)
    return _bound_rows(summary, "results", limit, offset)


@mcp.tool(
    description=(
        "Submit a BigFix client (fast) query and wait for results, polling "
        f"until done or timeout. {TARGETING_DESCRIPTION}"
    )
)
@bes_errors("client_query")
async def client_query(
    query_text: Annotated[
        str,
        Field(description="Client relevance to evaluate on each targeted agent."),
    ],
    ctx: Context,
    target_all: Annotated[bool, Field(description="Target all computers.")] = False,
    target_computer_ids: Annotated[
        list[int] | None, Field(description="Target these BigFix computer IDs.")
    ] = None,
    target_computer_names: Annotated[
        list[str] | None, Field(description="Target these computer names.")
    ] = None,
    target_relevance: Annotated[
        str | None,
        Field(description="Client relevance targeting expression."),
    ] = None,
    timeout_seconds: Annotated[
        float, Field(description=f"Max seconds to wait (1-{MAX_TIMEOUT_SECONDS}).")
    ] = 60,
    poll_interval_seconds: Annotated[
        float,
        Field(description=f"Seconds between polls (min {MIN_POLL_INTERVAL_SECONDS})."),
    ] = 5,
    stable_polls: Annotated[
        int,
        Field(
            description=(
                "Stop after this many consecutive polls with no new computers "
                "reporting (once at least one has)."
            )
        ),
    ] = 2,
    expected_count: Annotated[
        int | None,
        Field(
            description=(
                "Stop once this many computers reported. Defaults to the "
                "targeted computer count when knowable."
            )
        ),
    ] = None,
    limit: Annotated[int | None, LIMIT_FIELD] = None,
    offset: Annotated[int, OFFSET_FIELD] = 0,
) -> dict:
    """Submit a client fast query, poll for results, return them when done.

    Stops when the expected number of computers reported, results stop
    growing, or timeout - stop_reason in the response says which. Partial
    results at timeout are normal (offline agents never report). For very
    long waits, prefer client_query_submit + client_query_results.
    """
    # advisory preflight before spending up to timeout_seconds polling
    advisory = _client_query_advisory(query_text, target_relevance)
    conn = connection.get_connection()
    timeout_seconds = max(1, min(timeout_seconds, MAX_TIMEOUT_SECONDS))
    poll_interval_seconds = max(poll_interval_seconds, MIN_POLL_INTERVAL_SECONDS)

    target_xml, derived_count = clientquery.build_target_xml(
        target_all=target_all,
        computer_ids=target_computer_ids,
        computer_names=target_computer_names,
        target_relevance=target_relevance,
    )
    if expected_count is None:
        expected_count = derived_count

    query_id = clientquery.submit_client_query(conn, query_text, target_xml)
    # progress notifications (not ctx.info logging: the logging capability is
    # deprecated in the 2026-07-28 stateless protocol this server targets)
    await ctx.report_progress(
        progress=0,
        total=expected_count,
        message=f"Submitted client query {query_id}; polling for results.",
    )

    async def progress_cb(reported, expected, message):
        await ctx.report_progress(progress=reported, total=expected, message=message)

    summary = await clientquery.poll_client_query(
        conn,
        query_id,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        stable_polls=stable_polls,
        expected_count=expected_count,
        progress_cb=progress_cb,
    )
    return _with_advisory(_bound_rows(summary, "results", limit, offset), advisory)


@mcp.tool
@bes_errors("get_server_info")
def get_server_info() -> dict:
    """Get BigFix root server info (version, etc) from /api/serverinfo."""
    conn = connection.get_connection()
    result = check_rest_result(conn.get("serverinfo"), "get_server_info")
    return json.loads(result.text)


@mcp.tool
@bes_errors("list_sites")
def list_sites() -> dict:
    """List all sites visible to the configured operator.

    Returns {"data": <site listing>, "truncated": ..., "total_chars": ...}.
    A deployment subscribed to many external content sites can produce a large
    listing; when it exceeds the size cap, data is null and note says so
    rather than handing back a half-cut structure that looks complete.
    """
    conn = connection.get_connection()
    result = check_rest_result(conn.get("sites"), "list_sites")
    return response.bound_mapping(
        besxml.xml_to_dict(result.text),
        hint="Try session_relevance_query 'names of bes sites' instead.",
    )


@mcp.tool
@bes_errors("get_computer_group")
def get_computer_group(
    group_name: Annotated[str, Field(description="Name of the computer group.")],
    site_path: Annotated[
        str,
        Field(
            description=(
                "Site path containing the group, e.g. 'master', "
                "'custom/MySite', 'operator/SomeOperator'."
            )
        ),
    ],
) -> dict:
    """Look up a computer group by name within an explicit site path.

    Implemented against /api/computergroups/{site_path} directly rather
    than besapi's get_computergroup, which routes through the mutable
    "current site path" connection state this server avoids.
    """
    conn = connection.get_connection()
    group = content.get_computer_group_by_name(conn, group_name, site_path)
    if group is not None:
        return group
    raise ToolError(
        f"Computer group '{group_name}' not found in site '{site_path}' - it "
        "may not exist, or may not be visible to the configured operator."
    )


@mcp.tool
@bes_errors("get_computer")
def get_computer(
    computer_id: Annotated[int, Field(description="BigFix computer ID.")],
) -> dict:
    """Get one computer's full record from /api/computer/{id}.

    Includes the reported properties BigFix holds for that computer. Use
    find_computers first if you only know part of the name.
    """
    conn = connection.get_connection()
    result = check_rest_result(content.get_computer(conn, computer_id), "get_computer")
    return response.bound_mapping(
        besxml.xml_to_dict(result.text),
        hint="Use session_relevance_query to select specific properties instead.",
    )


@mcp.tool
@bes_errors("find_computers")
def find_computers(
    name_contains: Annotated[
        str, Field(description="Case-insensitive substring of the computer name.")
    ],
    limit: Annotated[int | None, LIMIT_FIELD] = None,
    offset: Annotated[int, OFFSET_FIELD] = 0,
) -> dict:
    """Find computers whose name contains a substring.

    Returns (name, id, last report time) rows. The search term cannot contain
    a double quote - BigFix relevance string literals have no escape for one.
    """
    relevance = content.build_computer_search_relevance(name_contains)
    envelope = sessionrelevance.evaluate(relevance)
    return _bound_rows(check_relevance_envelope(envelope), "result", limit, offset)


@mcp.tool
@bes_errors("applicable_fixlets")
def applicable_fixlets(
    computer_id: Annotated[int, Field(description="BigFix computer ID.")],
) -> dict:
    """Content currently relevant to one computer (/api/computer/{id}/fixlets).

    This is the patch/compliance question: what BigFix currently considers
    applicable to that machine.
    """
    conn = connection.get_connection()
    result = check_rest_result(
        content.get_computer_fixlets(conn, computer_id), "applicable_fixlets"
    )
    return response.bound_mapping(
        besxml.xml_to_dict(result.text),
        hint="Use session_relevance_query for a counted or filtered view of relevant content.",
    )


@mcp.tool
@bes_errors("get_action")
def get_action(
    action_id: Annotated[int, Field(description="BigFix action ID.")],
) -> dict:
    """Get an action's definition from /api/action/{id}."""
    conn = connection.get_connection()
    result = check_rest_result(actions.get_action(conn, action_id), "get_action")
    return response.bound_mapping(besxml.xml_to_dict(result.text))


@mcp.tool
@bes_errors("get_action_status")
def get_action_status(
    action_id: Annotated[int, Field(description="BigFix action ID.")],
) -> dict:
    """Get an action's per-computer execution state (/api/action/{id}/status).

    The "did my change actually work" tool. Status is scoped to the computers
    the configured operator can see.
    """
    conn = connection.get_connection()
    result = check_rest_result(actions.get_action_status(conn, action_id), "get_action_status")
    return response.bound_mapping(
        besxml.xml_to_dict(result.text),
        hint="For a large fleet, query action results with session relevance.",
    )


@mcp.tool
@bes_errors("list_actions")
def list_actions() -> dict:
    """List actions visible to the configured operator (/api/actions)."""
    conn = connection.get_connection()
    result = check_rest_result(actions.list_actions(conn), "list_actions")
    return response.bound_mapping(
        besxml.xml_to_dict(result.text),
        hint=(
            "Use session_relevance_query, e.g. "
            "'(name of it, id of it) of bes actions', for a narrower view."
        ),
    )


@mcp.tool
@bes_errors("find_content")
def find_content(
    kind: Annotated[
        str,
        Field(description="One of: fixlet, task, analysis, baseline."),
    ],
    name_contains: Annotated[
        str, Field(description="Case-insensitive substring of the content name.")
    ],
    limit: Annotated[int | None, LIMIT_FIELD] = None,
    offset: Annotated[int, OFFSET_FIELD] = 0,
) -> dict:
    """Search content by name across every site the operator can see.

    Returns rows of {name, id, site_name, site_path} - site_path is what
    get_content needs. It is null when the site name is ambiguous (two sites
    of different types sharing a display name), with the options listed under
    site_path_candidates rather than guessed at.

    The search term cannot contain a double quote.
    """
    relevance = content.build_content_search_relevance(kind, name_contains)
    envelope = check_relevance_envelope(sessionrelevance.evaluate(relevance))
    bounded = _bound_rows(envelope, "result", limit, offset)
    conn = connection.get_connection()
    # resolve site paths only for the rows actually being returned
    bounded["result"] = content.annotate_content_rows(
        bounded["result"], content.build_site_path_map(conn)
    )
    return bounded


@mcp.tool
@bes_errors("get_content")
def get_content(
    kind: Annotated[str, Field(description="One of: fixlet, task, analysis, baseline.")],
    site_path: Annotated[
        str,
        Field(
            description=(
                "Site path containing the content, e.g. 'master', "
                "'custom/MySite', 'operator/SomeOperator'."
            )
        ),
    ],
    content_id: Annotated[int, Field(description="Content ID within the site.")],
) -> dict:
    """Get one fixlet, task, analysis or baseline by site path and ID."""
    conn = connection.get_connection()
    result = check_rest_result(
        content.get_content(conn, kind, site_path, content_id), "get_content"
    )
    return response.bound_mapping(besxml.xml_to_dict(result.text))


@mcp.tool
@bes_errors("list_operators")
def list_operators() -> dict:
    """List console operators visible to the configured operator."""
    conn = connection.get_connection()
    result = check_rest_result(content.list_operators(conn), "list_operators")
    return response.bound_mapping(besxml.xml_to_dict(result.text))


@mcp.tool
@bes_errors("list_roles")
def list_roles() -> dict:
    """List BigFix roles visible to the configured operator."""
    conn = connection.get_connection()
    result = check_rest_result(content.list_roles(conn), "list_roles")
    return response.bound_mapping(besxml.xml_to_dict(result.text))


@mcp.tool
@bes_errors("validate_bes_xml")
def validate_bes_xml(
    bes_xml: Annotated[str, Field(description="BES XML document to check.")],
) -> dict:
    """Validate BES XML against the BigFix schemas.

    Makes no server call.

    Use before asking anyone to import content: it catches malformed or
    non-conforming XML locally instead of as a REST error.
    """
    return content.validate_bes_xml(bes_xml)


@mcp.tool
@bes_errors("get_operator")
def get_operator(
    user_name: Annotated[str, Field(description="BigFix operator user name.")],
) -> dict:
    """Look up a BigFix operator (console user) by name."""
    conn = connection.get_connection()
    user = conn.get_user(user_name)  # RESTResult, or None when not found
    if user is None:
        raise ToolError(
            f"Operator '{user_name}' not found - it may not exist, or may not "
            "be visible to the configured operator."
        )
    return besxml.xml_to_dict(user.text)


@mcp.tool
@bes_errors("get_dashboard_variable")
def get_dashboard_variable(
    dashboard_name: Annotated[str, Field(description="Dashboard name.")],
    var_name: Annotated[str, Field(description="Dashboard variable name.")],
) -> dict:
    """Read a BigFix dashboard datastore variable value (read-only)."""
    conn = connection.get_connection()
    # besapi returns the value as a plain string
    value = conn.get_dashboard_variable_value(dashboard_name, var_name)
    return {"dashboard": dashboard_name, "name": var_name, "value": value}


@mcp.tool
@bes_errors("whoami")
def whoami() -> dict:
    """Show the configured connection: user, root server, main operator status.

    writes_enabled says whether the gated write tools are registered in this
    process; when false they are absent from the tool list entirely.

    Cheap connectivity and permission smoke test. is_main_operator tells you
    whether results from the other tools can be treated as the full state of
    BigFix (master operator) or only as this operator's scoped view, which
    may be incomplete in ways this operator cannot detect.
    """
    conn = connection.get_connection()
    return {
        "username": conn.username,
        "rootserver": conn.rootserver,
        "is_main_operator": conn.am_i_main_operator(),
        "writes_enabled": WRITES_ENABLED,
        "qna_enabled": QNA_ENABLED,
        "besapi_version": besapi.besapi.__version__,
        "bigfix_root_mcp_version": __version__,
    }


@mcp.tool
@bes_errors("api_get")
def api_get(
    path: Annotated[
        str,
        Field(
            description=(
                "Relative REST API path under /api/, e.g. 'help', 'computers', "
                "'sites', 'computer/123'. Query strings allowed."
            )
        ),
    ],
    max_chars: Annotated[
        int | None,
        Field(
            description=(
                f"Max characters of response text (default {response.MAX_RESPONSE_CHARS})."
            )
        ),
    ] = None,
) -> dict:
    """Read-only escape hatch: GET any BigFix REST API path under /api/.

    GET is non-mutating across the BigFix REST API. Use path 'help' to
    discover available endpoints. Response text is capped at max_chars;
    total_chars reports the full length.
    """
    path = path.strip().lstrip("/")
    path = path.removeprefix("api/")
    if "://" in path or path.startswith("..") or not path:
        raise ToolError(
            "path must be a relative BigFix REST API path such as 'help' or 'computers'."
        )
    conn = connection.get_connection()
    result = check_rest_result(conn.get(path), "api_get")
    bounded = response.bound_text(result.text, max_chars=max_chars)
    return {
        "status_code": result.request.status_code,
        "content_type": result.request.headers.get("content-type", ""),
        **bounded,
    }


# --------------------------------------------------------------------------
# Gated write surface. Registered only when BIGFIX_ALLOW_WRITES is set, so
# with the gate off these tools do not exist as far as any client can tell.
# --------------------------------------------------------------------------

DRY_RUN_FIELD = Field(
    default=True,
    description=(
        "When true (the default) nothing is sent: the response describes the "
        "call that would be made. Pass false to actually perform the write."
    ),
)


def _audit(tool: str, target: str, dry_run: bool, outcome: str) -> None:
    """One structured stderr line per write attempt."""
    conn = connection.get_connection()
    logger.info(
        "BIGFIX WRITE tool=%s operator=%s rootserver=%s target=%s dry_run=%s outcome=%s",
        tool,
        conn.username,
        conn.rootserver,
        target,
        dry_run,
        outcome,
    )


# --------------------------------------------------------------------------
# qna fast evaluation. Registration is the gate, as with writes: with every
# target source disabled (or the package missing), these tools do not exist
# as far as any client can tell.
# --------------------------------------------------------------------------

if QNA_ENABLED:

    async def _discover_qna(ctx: Context, *, only_if_needed: bool = False) -> dict | None:
        """Run auto-discovery with progress and audit lines; {path, added}.

        None when only_if_needed and a concurrent discovery got there first.
        """
        await ctx.report_progress(progress=0, message="Running qna auto-discovery.")
        # audit lines, like a run's: discovery runs qna locally, in containers
        # and on developer.bigfix.com, and writes ~/.bigfix/remote_clients.toml.
        # Logged before probing, since a failure can come after the probes.
        logger.info("BIGFIX QNA auto-discovery start path=%s", qna.user_inventory_path())
        try:
            added = await qna.run_discovery(only_if_needed=only_if_needed)
        except Exception as exc:
            logger.info("BIGFIX QNA auto-discovery failed: %s", exc)
            raise
        if added is None:
            logger.info("BIGFIX QNA auto-discovery skipped: an inventory exists now")
            return None
        path = qna.inventory_path() or None
        logger.info("BIGFIX QNA auto-discovery path=%s added=%s", path, added)
        return {"path": path, "added": added}

    @mcp.tool
    @bes_errors("qna_list_targets")
    def qna_list_targets() -> dict:
        """List targets available to qna_eval_client_relevance.

        inventory_path is the inventory file in use: BIGFIX_QNA_INVENTORY
        when the server sets it, else the default search
        (~/.bigfix/remote_clients.toml, then the all-users config directory);
        null when none was found. A run that names no targets uses every
        inventory host. auto_discovery says whether qna_discover_targets is
        available (it probes local qna, container images and
        developer.bigfix.com, and adds what works to
        ~/.bigfix/remote_clients.toml); auto_discovery_pending says no
        inventory file exists yet, so the next run naming no targets
        discovers first without being asked. containers_allowed says whether
        arbitrary container image names may be passed as targets. Listing
        never probes or writes anything.
        """
        return {
            "inventory": qna.list_inventory_targets(),
            "inventory_path": qna.inventory_path() or None,
            "inventory_source": qna.inventory_source(),
            "auto_discovery": qna.discovery_allowed(),
            "auto_discovery_pending": qna.discovery_needed(),
            "containers_allowed": qna.containers_enabled(),
        }

    @mcp.tool
    @bes_errors("qna_eval_client_relevance")
    async def qna_eval_client_relevance(
        relevance: Annotated[
            str,
            Field(description="Client relevance to evaluate with qna on each target."),
        ],
        ctx: Context,
        inventory_targets: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Names from the server's qna inventory (see qna_list_targets). "
                    "Leave both this and container_images empty to run on every "
                    "inventory host."
                )
            ),
        ] = None,
        container_images: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Container images to evaluate in, e.g. 'ubuntu:22.04'. The first "
                    "use of an image is slow (agent artifact download + derived image "
                    "build); later runs are about a second."
                )
            ),
        ] = None,
        qna_version: Annotated[
            str | None,
            Field(
                description=(
                    "BigFix agent/qna version to provision on targets, e.g. '11.0'. "
                    "Required for plain container images (they have no qna baked "
                    "in); the first use of a version downloads and caches it."
                )
            ),
        ] = None,
        timeout_seconds: Annotated[
            float,
            Field(description=f"Per-target timeout (1-{qna.MAX_QNA_TIMEOUT_SECONDS})."),
        ] = 30,
        limit: Annotated[int | None, LIMIT_FIELD] = None,
        offset: Annotated[int, OFFSET_FIELD] = 0,
    ) -> dict:
        """Evaluate CLIENT relevance with the BigFix qna tool - a fast feedback
        loop that never touches the BigFix deployment.

        qna runs on lab targets (containers or admin-inventoried hosts), NOT on
        managed BigFix endpoints: results reflect each target machine only.
        There is no operator scope, no site subscriptions and no client
        settings, so inspectors that depend on deployment state will differ
        from what client_query returns. Iterate on relevance syntax and
        inspector behavior here, then confirm on real agents with client_query.

        With no inventory_targets or container_images, it runs on every
        inventory host (qna only reads). An online_evaluator inventory host
        sends the client relevance to developer.bigfix.com; name targets
        to keep a query off it. When no inventory file exists yet
        (qna_list_targets: auto_discovery_pending), such a run first
        discovers what works on the server's host and records it in
        ~/.bigfix/remote_clients.toml; once the file exists, discovery runs
        only through qna_discover_targets. The payload's auto_discovery says
        what was added. A discovering run can take minutes.

        Per-target failures come back as result rows with error_kind
        ('relevance', 'qna', 'bootstrap', 'transport', 'resolve'), never as a
        tool error. ok_count and target_count describe the whole run; the rows
        are windowed by limit/offset.
        """
        advisory = analysis.preflight(relevance, dialect="client")
        discovered = None
        if not inventory_targets and not container_images and qna.discovery_needed():
            discovered = await _discover_qna(ctx, only_if_needed=True)
            if discovered is not None and not discovered["added"]:
                raise ValueError(
                    "no targets: qna auto-discovery found nothing that works on this "
                    "server; pass container_images, or add hosts to "
                    f"{discovered['path'] or 'the inventory'} by hand, or fix what "
                    "kept discovery from finding any and run qna_discover_targets."
                )
        targets = qna.resolve_targets(inventory_targets, container_images)
        timeout_seconds = max(1, min(timeout_seconds, qna.MAX_QNA_TIMEOUT_SECONDS))
        pkg = qna._qna()  # the module's one lazy import seam
        # audit line, mirroring _audit for writes: qna executes on targets
        logger.info(
            "BIGFIX QNA relevance=%r targets=%s",
            relevance[:200],
            [target.label for target in targets],
        )
        total = pkg.count_work(targets, qna_version)
        await ctx.report_progress(
            progress=0,
            total=total,
            message=f"Evaluating on {len(targets)} target(s).",
        )
        results = []
        async for result in pkg.evaluate_client_relevance_stream(
            relevance,
            targets,
            qna_version=qna_version,
            max_parallel=min(qna.MAX_PARALLEL, len(targets)),
            timeout_s=timeout_seconds,
        ):
            results.append(qna.shape_result(result))
            status = "ok" if results[-1].get("error_kind") is None else results[-1]["error_kind"]
            await ctx.report_progress(
                progress=len(results),
                total=total,
                message=f"{results[-1].get('host')}: {status}",
            )
        payload = {
            "target_count": len(targets),
            "ok_count": sum(1 for r in results if r.get("error_kind") is None),
            "results": results,
        }
        if discovered is not None:
            payload["auto_discovery"] = discovered
        if not results:
            # error rows count as results; only a run that returned none at
            # all gets pointed at discovery
            payload["hint"] = "No target returned a result." + qna.discovery_hint()
        return _with_advisory(_bound_rows(payload, "results", limit, offset), advisory)


# registered only where discovery may run (default inventory, not switched
# off): with a BIGFIX_QNA_INVENTORY path the tool does not exist, like writes
if QNA_ENABLED and qna.discovery_enabled():

    @mcp.tool
    @bes_errors("qna_discover_targets")
    async def qna_discover_targets(ctx: Context) -> dict:
        """Find qna targets that work on the server's host and add them to the
        inventory, like the bigfix-remote-client-relevance CLI's
        --auto-discovery. Evaluates nothing.

        Probes this host's qna, a downloaded qna, the container engine's
        images and developer.bigfix.com, then adds what works and is not
        already listed to ~/.bigfix/remote_clients.toml. Never uses SSH
        discovery and never prompts. Slow: it can take minutes. Use it when
        qna_list_targets shows no (or too few) inventory hosts; a run of
        qna_eval_client_relevance naming no targets only discovers by
        itself when no inventory file exists yet.

        Returns path (the inventory written), added (new host names) and
        inventory (every target now listed).
        """
        # rechecked per call: the gate is read at import, the environment
        # can differ by now
        if refusal := qna.discovery_refusal():
            raise ValueError(refusal)
        discovered = await _discover_qna(ctx)
        if discovered is None:  # only only_if_needed returns None
            raise RuntimeError("qna auto-discovery returned no outcome")
        return {**discovered, "inventory": qna.list_inventory_targets()}


if WRITES_ENABLED:

    @mcp.tool
    @bes_errors("stop_action")
    def stop_action(
        action_id: Annotated[int, Field(description="BigFix action ID to stop.")],
        dry_run: Annotated[bool, DRY_RUN_FIELD] = True,
    ) -> dict:
        """Stop an in-flight BigFix action (POST /api/action/{id}/stop).

        Stopping prevents further execution; it does not undo what already
        ran on endpoints that have already reported. Check get_action_status
        first to see how far it got.
        """
        conn = connection.get_connection()
        path = writes.stop_action_path(action_id)
        if dry_run:
            _audit("stop_action", path, True, "not_sent")
            return {
                "dry_run": True,
                "would_call": f"POST {path}",
                "note": "Pass dry_run=false to actually stop this action.",
            }
        result = writes.stop_action(conn, action_id)
        _audit("stop_action", path, False, str(result.request.status_code))
        return {
            "dry_run": False,
            "action_id": int(action_id),
            "status_code": result.request.status_code,
            **response.bound_text(result.text),
        }

    @mcp.tool
    @bes_errors("set_dashboard_variable")
    def set_dashboard_variable(
        dashboard_name: Annotated[str, Field(description="Dashboard name.")],
        var_name: Annotated[str, Field(description="Dashboard variable name.")],
        var_value: Annotated[str, Field(description="Value to store.")],
        private: Annotated[bool, Field(description="Store as a private variable.")] = False,
        dry_run: Annotated[bool, DRY_RUN_FIELD] = True,
    ) -> dict:
        """Set a BigFix dashboard datastore variable.

        Touches the datastore only - nothing on any managed endpoint changes.
        Read the current value with get_dashboard_variable first if you intend
        to preserve it; this overwrites.
        """
        conn = connection.get_connection()
        path = writes.dashboard_variable_path(dashboard_name, var_name)
        payload = writes.build_dashboard_variable_xml(dashboard_name, var_name, var_value, private)
        if dry_run:
            _audit("set_dashboard_variable", path, True, "not_sent")
            return {
                "dry_run": True,
                "would_call": f"POST {path}",
                "payload": payload,
                "note": "Pass dry_run=false to actually write this value.",
            }
        result = writes.set_dashboard_variable(conn, dashboard_name, var_name, var_value, private)
        _audit("set_dashboard_variable", path, False, str(result.request.status_code))
        return {
            "dry_run": False,
            "dashboard": dashboard_name,
            "name": var_name,
            "status_code": result.request.status_code,
        }

    @mcp.tool
    @bes_errors("import_bes_content")
    def import_bes_content(
        site_path: Annotated[
            str,
            Field(
                description=(
                    "Target site path, e.g. 'custom/MySite'. Import targets a custom site you own."
                )
            ),
        ],
        bes_xml: Annotated[str, Field(description="BES XML document to import.")],
        dry_run: Annotated[bool, DRY_RUN_FIELD] = True,
    ) -> dict:
        """Create or update custom content in a site (POST /api/import/{site}).

        Importing content does NOT run it: a fixlet or task created this way
        does nothing until someone deploys an action against it in the
        console. That is deliberate - this server has no action-deployment
        tool.

        The XML is validated against the BigFix schemas first, in dry-run too,
        so malformed content fails locally rather than as a REST error.
        """
        conn = connection.get_connection()
        path = writes.import_path(site_path)
        verdict = content.validate_bes_xml(bes_xml)
        if not verdict["valid"]:
            _audit("import_bes_content", path, dry_run, "invalid_xml")
            raise ToolError(
                f"import_bes_content: bes_xml is not valid BES content: {verdict['reason']}"
            )
        if dry_run:
            _audit("import_bes_content", path, True, "not_sent")
            return {
                "dry_run": True,
                "would_call": f"POST {path}",
                "xml_valid": True,
                "note": "Pass dry_run=false to actually import this content.",
            }
        result = writes.import_bes_content(conn, site_path, bes_xml)
        _audit("import_bes_content", path, False, str(result.request.status_code))
        return {
            "dry_run": False,
            "site_path": site_path,
            "status_code": result.request.status_code,
            **response.bound_text(result.text),
        }


def main() -> None:
    """Entry point: stderr-only logging, then serve MCP over stdio or HTTP.

    BIGFIX_MCP_TRANSPORT=http serves streamable HTTP on FastMCP's defaults
    (127.0.0.1:8000). There is no auth layer here: anyone who can reach the
    port acts as the configured operator, so put authentication in front
    before binding beyond localhost.
    """
    # stdout belongs to the MCP stdio transport; all logging (including the
    # besapi logger, which propagates to root) must go to stderr.
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    transport = os.environ.get("BIGFIX_MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run(show_banner=False)
    elif transport == "http":
        # stdout is free over HTTP, so the banner (with the URL) is useful
        mcp.run(transport="http", show_banner=True)
    else:
        raise SystemExit(f"BIGFIX_MCP_TRANSPORT must be 'stdio' or 'http', got {transport!r}")


if __name__ == "__main__":
    main()
