"""Tests for the Streamable-HTTP MCP transport.

Everything here runs against an ``httpx.MockTransport``, so no test touches the network
or a live Cloud Run service. What is pinned is the part of the protocol that was learned
the hard way against the real ``dailies-mcp-grafana`` service and that fails opaquely
when it is wrong:

- the session id arrives in the ``Mcp-Session-Id`` **response header** of ``initialize``
  and has to be echoed on every later request, or the server answers 400 with no clue;
- ``notifications/initialized`` is sent before any ``tools/call``;
- a response may be SSE-framed (``data: {...}``) rather than plain JSON;
- ``Accept`` must offer both media types;
- the request is never sent unauthenticated. Off Cloud Run there is no metadata server,
  and a 401 from Cloud Run's frontend says nothing about why.
"""

import base64
import json
import logging

import httpx
import pytest
from dailies_api.mcp_client import GrafanaMCP
from dailies_api.mcp_transport import (
    FALLBACK_TTL_SECONDS,
    ID_TOKEN_ENV,
    METADATA_ATTEMPTS,
    IdentityTokenSource,
    IdentityTokenUnavailable,
    MCPProtocolError,
    StreamableHTTPSession,
    connect,
)

# A reserved-TLD host, as in tests/api/test_diagnose_route.py. Nothing here resolves
# anything, and the live service URL carries the GCP project number.
MCP_URL = "https://dailies-mcp-grafana.example.invalid"
SESSION_ID = "5f3b0c1e-session"


def jwt(expires_at: float) -> str:
    """A token shaped like a Cloud Run identity token: only ``exp`` is ever read."""
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires_at}).encode()).rstrip(b"=")
    return f"header.{payload.decode()}.signature"


class StaticToken:
    """A token source that never leaves the process."""

    def __init__(self, value="test-id-token"):
        self.value = value
        self.calls = 0

    async def token(self):
        self.calls += 1
        return self.value


class Recorder:
    """Answers MCP requests from canned results and records what was asked."""

    def __init__(self, results=None, *, sse=False, session_id=SESSION_ID):
        self.requests = []
        self.results = results or {}
        self.sse = sse
        self.session_id = session_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request, body))
        method = body.get("method", "")
        if method == "initialize":
            return self._reply(
                body,
                {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {}},
                headers={"Mcp-Session-Id": self.session_id},
            )
        if method.startswith("notifications/"):
            return httpx.Response(202)
        return self._reply(body, self.results.get(method, {}))

    def _reply(self, body, result, headers=None):
        payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
        if self.sse:
            text = f"event: message\ndata: {json.dumps(payload)}\n\n"
            return httpx.Response(
                200, text=text, headers={**(headers or {}), "content-type": "text/event-stream"}
            )
        return httpx.Response(200, json=payload, headers=headers or {})

    def sent(self, method):
        return [(request, body) for request, body in self.requests if body.get("method") == method]

    @property
    def methods(self):
        return [body.get("method") for _, body in self.requests]


def client_for(recorder: Recorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(recorder.handler))


async def opened(recorder, **kwargs):
    session = StreamableHTTPSession(
        MCP_URL, client=client_for(recorder), token_source=StaticToken(), **kwargs
    )
    await session.initialize()
    return session


# -- the handshake ---------------------------------------------------------------


async def test_initialize_reads_the_session_id_from_the_response_header():
    recorder = Recorder()
    session = await opened(recorder)
    assert session.session_id == SESSION_ID


async def test_initialized_notification_is_sent_before_any_tool_call():
    recorder = Recorder()
    session = await opened(recorder)
    await session.call_tool("query_prometheus", {"expr": "up"})
    assert recorder.methods == ["initialize", "notifications/initialized", "tools/call"]


async def test_the_session_id_header_is_sent_on_every_request_after_initialize():
    recorder = Recorder()
    session = await opened(recorder)
    await session.call_tool("query_prometheus", {"expr": "up"})

    initialize, _ = recorder.sent("initialize")[0]
    assert "Mcp-Session-Id" not in initialize.headers
    for request, _ in recorder.requests[1:]:
        assert request.headers["Mcp-Session-Id"] == SESSION_ID


async def test_requests_go_to_the_mcp_endpoint():
    recorder = Recorder()
    await opened(recorder)
    request, _ = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{MCP_URL}/mcp"


async def test_a_url_that_already_names_the_endpoint_is_not_doubled():
    recorder = Recorder()
    session = StreamableHTTPSession(
        f"{MCP_URL}/mcp", client=client_for(recorder), token_source=StaticToken()
    )
    await session.initialize()
    assert str(recorder.requests[0][0].url) == f"{MCP_URL}/mcp"


async def test_accept_offers_both_json_and_event_stream():
    recorder = Recorder()
    await opened(recorder)
    accept = recorder.requests[0][0].headers["Accept"]
    assert "application/json" in accept
    assert "text/event-stream" in accept


async def test_calling_before_initialize_says_so():
    session = StreamableHTTPSession(
        MCP_URL, client=client_for(Recorder()), token_source=StaticToken()
    )
    with pytest.raises(MCPProtocolError, match="initialize"):
        await session.call_tool("query_prometheus", {"expr": "up"})


# -- response framing ------------------------------------------------------------


TOOL_RESULT = {"content": [{"type": "text", "text": '{"data":{"result":[]}}'}], "isError": False}


def one_shot_handler(answer):
    """Answers initialize normally and everything else with ``answer(body)``."""

    def handler(request):
        body = json.loads(request.content)
        method = body.get("method", "")
        if method == "initialize":
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "x"}},
                headers={"Mcp-Session-Id": SESSION_ID},
            )
        if method.startswith("notifications/"):
            return httpx.Response(202)
        return answer(body)

    return handler


def session_on(handler):
    return StreamableHTTPSession(
        MCP_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_source=StaticToken(),
    )


async def test_an_sse_framed_response_is_parsed():
    recorder = Recorder({"tools/call": TOOL_RESULT}, sse=True)
    session = await opened(recorder)
    result = await session.call_tool("query_prometheus", {"expr": "up"})
    assert result.content[0].text == '{"data":{"result":[]}}'
    assert result.isError is False


async def test_a_plain_json_response_is_parsed():
    recorder = Recorder({"tools/call": TOOL_RESULT})
    session = await opened(recorder)
    result = await session.call_tool("query_prometheus", {"expr": "up"})
    assert result.content[0].text == '{"data":{"result":[]}}'


async def test_an_sse_stream_that_answers_something_else_first_is_skipped():
    """The server may send progress notifications on the same stream as the answer."""

    def answer(body):
        noise = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": TOOL_RESULT}
        text = f"data: {json.dumps(noise)}\n\ndata: {json.dumps(reply)}\n\n"
        return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})

    session = session_on(one_shot_handler(answer))
    await session.initialize()
    result = await session.call_tool("query_prometheus", {"expr": "up"})
    assert result.content[0].text == '{"data":{"result":[]}}'


async def test_a_jsonrpc_error_is_raised_with_the_servers_message():
    def answer(body):
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "error": {"code": -32602, "message": "unknown tool"},
            },
        )

    session = session_on(one_shot_handler(answer))
    await session.initialize()
    with pytest.raises(MCPProtocolError, match="unknown tool"):
        await session.call_tool("nope", {})


async def test_an_http_error_names_the_status():
    session = session_on(lambda request: httpx.Response(401, text="Unauthorized"))
    with pytest.raises(MCPProtocolError, match="401"):
        await session.initialize()


# -- the shape the Grafana wrapper consumes --------------------------------------


async def test_the_wrapper_reads_this_transport_without_adaptation():
    """The whole point: GrafanaMCP takes this session as-is."""
    recorder = Recorder(
        {"tools/call": {"content": [{"type": "text", "text": '{"data":{"result":[1]}}'}]}},
        sse=True,
    )
    session = await opened(recorder)
    grafana = GrafanaMCP(session, prometheus_uid="grafanacloud-prom")
    payload = await grafana.query_prometheus("up", start_time="now-30m")
    assert payload == {"data": {"result": [1]}}
    _, body = recorder.sent("tools/call")[0]
    assert body["params"]["name"] == "query_prometheus"
    assert body["params"]["arguments"]["datasourceUid"] == "grafanacloud-prom"


async def test_list_tools_reads_the_server_listing():
    recorder = Recorder({"tools/list": {"tools": [{"name": "query_prometheus"}]}})
    session = await opened(recorder)
    listing = await session.list_tools()
    assert [tool.name for tool in listing.tools] == ["query_prometheus"]


# -- authentication --------------------------------------------------------------


class MetadataStub:
    def __init__(self, *, tokens=None, available=True):
        self.tokens = list(tokens or [jwt(10_000)])
        self.available = available
        self.requests = []

    def handler(self, request):
        if not self.available:
            raise httpx.ConnectError("no metadata server", request=request)
        self.requests.append(request)
        return httpx.Response(200, text=self.tokens.pop(0) if self.tokens else jwt(10_000))


class ScriptedMetadata:
    """A metadata server that answers a scripted sequence, so the failure paths can run.

    ``MetadataStub`` above only ever answers 200 or refuses to resolve, which left the
    two branches that actually happen inside a container - a non-200 and a timeout -
    with no coverage at all. Each entry here is an ``httpx.Response`` to return or an
    exception to raise; running past the end repeats the last entry, so a retry test
    does not have to know how many attempts the source makes.
    """

    def __init__(self, *script):
        self.script = list(script)
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        entry = self.script[min(len(self.requests) - 1, len(self.script) - 1)]
        if isinstance(entry, BaseException):
            raise entry
        return entry


def token_source(stub, *, env=None, now=None, **kwargs):
    return IdentityTokenSource(
        MCP_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)),
        env={} if env is None else env,
        now=(lambda: 0.0) if now is None else now,
        # Zero, so a retry test does not spend the real backoff.
        retry_delay_seconds=0.0,
        **kwargs,
    )


async def test_the_id_token_is_minted_for_the_mcp_audience():
    stub = MetadataStub()
    await token_source(stub).token()
    request = stub.requests[0]
    assert request.headers["Metadata-Flavor"] == "Google"
    assert "/instance/service-accounts/default/identity" in request.url.path
    assert request.url.params["audience"] == MCP_URL


async def test_the_id_token_is_cached_rather_than_minted_per_call():
    stub = MetadataStub()
    source = token_source(stub)
    assert await source.token() == await source.token()
    assert len(stub.requests) == 1


async def test_the_id_token_is_refreshed_before_it_expires():
    stub = MetadataStub(tokens=[jwt(300), jwt(10_000)])
    clock = {"now": 0.0}
    source = token_source(stub, now=lambda: clock["now"])
    first = await source.token()
    clock["now"] = 250.0  # inside the refresh margin, before the actual expiry
    second = await source.token()
    assert first != second
    assert len(stub.requests) == 2


async def test_the_env_var_is_used_when_there_is_no_metadata_server():
    stub = MetadataStub(available=False)
    source = token_source(stub, env={ID_TOKEN_ENV: "token-from-the-env"})
    assert await source.token() == "token-from-the-env"


async def test_no_identity_source_at_all_says_so_instead_of_401ing():
    stub = MetadataStub(available=False)
    with pytest.raises(IdentityTokenUnavailable) as raised:
        await token_source(stub, env={}).token()
    message = str(raised.value)
    assert ID_TOKEN_ENV in message
    assert "metadata" in message.lower()


# -- the metadata server's failure paths -----------------------------------------
#
# The metadata path is the ONLY one that can mint a token inside Cloud Run, and until
# these tests existed its two in-container failure modes - a non-200 and a timeout - had
# no coverage: the stub above only answers 200 or refuses to resolve. What shipped was a
# discarded status code, no log line at all, and a fixed message telling the operator the
# process "is not running on Cloud Run" while it was running on Cloud Run.


async def test_a_refusing_metadata_server_reports_what_it_actually_answered():
    stub = ScriptedMetadata(httpx.Response(403, text="Forbidden: no default service account"))

    with pytest.raises(IdentityTokenUnavailable) as raised:
        await token_source(stub, env={}).token()

    message = str(raised.value)
    assert "403" in message
    assert "no default service account" in message
    # The old fixed sentence, and inside the container it is the opposite of the truth.
    assert "not running on Cloud Run" not in message
    # A 4xx is a decision about this service account or audience, not a transient.
    assert len(stub.requests) == 1


async def test_a_refusing_metadata_server_is_logged(caplog):
    """The response body reaches one caller; the log is where an operator can find it."""
    stub = ScriptedMetadata(httpx.Response(403, text="Forbidden"))

    with (
        caplog.at_level(logging.WARNING, logger="dailies_api.mcp_transport"),
        pytest.raises(IdentityTokenUnavailable),
    ):
        await token_source(stub, env={}).token()

    assert any("403" in record.getMessage() for record in caplog.records)


async def test_a_5xx_from_the_metadata_server_is_retried():
    stub = ScriptedMetadata(httpx.Response(500, text="internal error"))

    with pytest.raises(IdentityTokenUnavailable) as raised:
        await token_source(stub, env={}).token()

    assert len(stub.requests) == METADATA_ATTEMPTS
    assert "500" in str(raised.value)


async def test_a_startup_transient_resolves_on_the_retry():
    """A container that has only just started can get a 5xx from a healthy metadata server."""
    good = jwt(10_000)
    stub = ScriptedMetadata(httpx.Response(503, text="starting up"), httpx.Response(200, text=good))

    assert await token_source(stub, env={}).token() == good
    assert len(stub.requests) == 2


async def test_a_timeout_is_retried_rather_than_treated_as_a_verdict():
    good = jwt(10_000)
    stub = ScriptedMetadata(httpx.ReadTimeout("slow"), httpx.Response(200, text=good))

    assert await token_source(stub, env={}).token() == good
    assert len(stub.requests) == 2


async def test_a_timeout_does_not_switch_the_metadata_server_off_for_good():
    """Latching on a timeout would leave a container with no token path at all.

    A timeout says the request was slow, not that the host has no metadata server. The
    verdict has to stay revisable, or one slow moment during startup costs every
    subsequent refresh for the life of the process.
    """
    good = jwt(10_000)
    slow = httpx.ReadTimeout("slow")
    stub = ScriptedMetadata(slow, slow, slow, httpx.Response(200, text=good))
    clock = {"now": 0.0}
    source = token_source(stub, env={ID_TOKEN_ENV: "token-from-the-env"}, now=lambda: clock["now"])

    # This run exhausts its attempts and falls back to the env var.
    assert await source.token() == "token-from-the-env"
    assert len(stub.requests) == METADATA_ATTEMPTS

    # The env token carries no readable exp, so it ages out on the fallback TTL.
    clock["now"] = FALLBACK_TTL_SECONDS + 1
    assert await source.token() == good
    assert len(stub.requests) == METADATA_ATTEMPTS + 1


async def test_a_connect_error_is_a_verdict_and_is_not_asked_again():
    """The contrast: a name that does not resolve cannot start to mid-process."""
    stub = ScriptedMetadata(httpx.ConnectError("no metadata server"))
    clock = {"now": 0.0}
    source = token_source(stub, env={ID_TOKEN_ENV: "token-from-the-env"}, now=lambda: clock["now"])

    assert await source.token() == "token-from-the-env"
    clock["now"] = FALLBACK_TTL_SECONDS + 1
    assert await source.token() == "token-from-the-env"

    assert len(stub.requests) == 1


async def test_a_connect_error_still_says_this_is_not_cloud_run():
    stub = ScriptedMetadata(httpx.ConnectError("no metadata server"))

    with pytest.raises(IdentityTokenUnavailable) as raised:
        await token_source(stub, env={}).token()

    assert "not running on Cloud Run" in str(raised.value)


async def test_the_token_audience_drops_an_endpoint_path_from_the_url():
    """Cloud Run checks `aud` against the service URL, which has no `/mcp` on it.

    `connect` documents that a URL already naming the endpoint is accepted, so minting
    against the raw string produced a token for `https://host/mcp` - rejected with the
    same bare 401 as no token at all, which is the failure this module exists to avoid.
    """
    recorder = Recorder()
    stub = ScriptedMetadata(httpx.Response(200, text=jwt(10_000)))

    def handler(request):
        if request.url.host == "metadata.google.internal":
            return stub.handler(request)
        return recorder.handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    async with connect(f"{MCP_URL}/mcp", client=client) as session:
        assert session.url == f"{MCP_URL}/mcp"

    assert stub.requests[0].url.params["audience"] == MCP_URL


async def test_an_unauthenticated_request_is_never_sent():
    """The failure has to happen before the wire, not as an opaque Cloud Run 401."""
    recorder = Recorder()
    stub = MetadataStub(available=False)
    session = StreamableHTTPSession(
        MCP_URL, client=client_for(recorder), token_source=token_source(stub, env={})
    )
    with pytest.raises(IdentityTokenUnavailable):
        await session.initialize()
    assert recorder.requests == []


async def test_every_request_carries_the_bearer_token():
    recorder = Recorder()
    session = await opened(recorder)
    await session.call_tool("query_prometheus", {"expr": "up"})
    for request, _ in recorder.requests:
        assert request.headers["Authorization"] == "Bearer test-id-token"


# -- lifecycle -------------------------------------------------------------------


async def test_connect_hands_back_an_initialized_session_and_closes_it():
    recorder = Recorder()
    async with connect(
        MCP_URL, client=client_for(recorder), token_source=StaticToken()
    ) as opened_session:
        assert opened_session.session_id == SESSION_ID
    assert "initialize" in recorder.methods
    assert opened_session.closed is True
