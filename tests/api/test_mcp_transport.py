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

import httpx
import pytest
from dailies_api.mcp_client import GrafanaMCP
from dailies_api.mcp_transport import (
    ID_TOKEN_ENV,
    IdentityTokenSource,
    IdentityTokenUnavailable,
    MCPProtocolError,
    StreamableHTTPSession,
    connect,
)

MCP_URL = "https://dailies-mcp-grafana-362568387922.us-central1.run.app"
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


def token_source(stub, *, env=None, now=None):
    return IdentityTokenSource(
        MCP_URL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(stub.handler)),
        env={} if env is None else env,
        now=(lambda: 0.0) if now is None else now,
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
