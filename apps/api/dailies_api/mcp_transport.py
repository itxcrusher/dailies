"""A Streamable-HTTP MCP session for the Grafana MCP server on Cloud Run.

:mod:`dailies_api.mcp_client` deliberately takes its session by injection, so that every
one of its tests runs without a Grafana. This module is the other half of that decision:
the one place that actually speaks the protocol over the wire, so the wrapper above it
and the agent above that stay transport-agnostic and testable.

It is hand-written rather than delegating to the MCP SDK's client because the SDK's
Streamable-HTTP client owns its own task group and reconnect loop, and the service on the
other end is a plain request/response Cloud Run container behind an IAM check. What is
actually needed is four HTTP POSTs with the right headers, and getting those headers
wrong is the whole difficulty. Each of the following was learned against the live
``dailies-mcp-grafana`` service on 2026-08-29, and every one of them fails as an opaque
400 or 401 with nothing in the body pointing at the cause:

**The session id is a response header.** ``initialize`` answers with ``Mcp-Session-Id``
in its *headers*, not in the JSON result. Every subsequent request must echo it. Reading
the body for it finds nothing, and the server then rejects the next call for having no
session.

**``notifications/initialized`` is not optional.** The server holds the session
un-negotiated until it arrives, and answers ``tools/call`` with an error that talks about
initialization rather than about the tool.

**The response may be SSE.** The same endpoint answers either ``application/json`` or
``text/event-stream`` depending on the request, so ``Accept`` has to offer both and the
reader has to cope with ``data: `` frames. A stream may also carry notifications before
the answer, so the frame whose ``id`` matches the request is the one to take, not the
first one that parses.

**Auth is an ID token, not an access token.** Cloud Run service-to-service auth needs a
Google-signed *identity* token whose ``audience`` is the target service's URL. The one
place that can mint it is the metadata server, using the runtime service account. A user
credential cannot: ``gcloud auth print-identity-token`` on a user account produces a token
with a fixed audience, which Cloud Run rejects. See :class:`IdentityTokenSource`.

The result objects returned here are structural matches for what
:class:`dailies_api.mcp_client.GrafanaMCP` reads - ``content`` blocks carrying ``type`` /
``text`` / ``data`` / ``mimeType``, and ``isError`` - rather than the SDK's pydantic
models. That is the same contract the wrapper's own tests fake, so the wrapper needs no
adaptation to sit on top of this.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

__all__ = [
    "ID_TOKEN_ENV",
    "MCP_ENDPOINT_PATH",
    "METADATA_ATTEMPTS",
    "METADATA_IDENTITY_URL",
    "METADATA_RETRY_DELAY_SECONDS",
    "PROTOCOL_VERSION",
    "ContentBlock",
    "IdentityTokenSource",
    "IdentityTokenUnavailable",
    "MCPProtocolError",
    "MCPTransportError",
    "StreamableHTTPSession",
    "TokenSource",
    "ToolDescriptor",
    "ToolListing",
    "ToolResult",
    "connect",
]

#: Where an ID token comes from outside Cloud Run. Set it to a token minted by something
#: that *can* mint one for this audience - in practice
#: ``gcloud auth print-identity-token --impersonate-service-account=<runtime sa>
#: --audiences=<mcp url>`` - to drive the live server from a laptop.
ID_TOKEN_ENV = "GOOGLE_ID_TOKEN"

#: The GCE metadata server's identity endpoint. Reachable from inside Cloud Run and
#: nowhere else; the hostname does not resolve on a developer machine.
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)

#: The path the Streamable-HTTP transport listens on. ``mcp-grafana -t streamable-http``
#: serves ``/mcp``; the service URL alone answers 404.
MCP_ENDPOINT_PATH = "/mcp"

#: The MCP protocol revision this client speaks. The server answers ``initialize`` with
#: the version it settled on, and that one is echoed back on later requests.
PROTOCOL_VERSION = "2025-06-18"

#: Refresh this many seconds before an ID token's ``exp``. An investigation is several
#: sequential tool calls over a minute or two, so a token that is about to expire when
#: the run starts must be replaced before it, not during it.
REFRESH_MARGIN_SECONDS = 300.0

#: What a token with an unreadable ``exp`` is assumed to be good for. Cloud Run identity
#: tokens last an hour; assuming five minutes means a needless re-mint rather than a
#: request that 401s halfway through an investigation.
FALLBACK_TTL_SECONDS = 300.0

#: How many times the metadata server's identity endpoint is asked when it answers 5xx or
#: times out. google-auth's own ``_metadata.get`` retries this endpoint for the same
#: reason: a container that has only just started can get a 500 or a timeout from a
#: metadata server that answers fine a moment later, and a startup-time transient must not
#: become an investigation that reports the service as unreachable.
METADATA_ATTEMPTS = 3

#: How long to wait between those attempts.
METADATA_RETRY_DELAY_SECONDS = 0.25

_CLIENT_INFO = {"name": "dailies", "version": "0.1.0"}

#: Named after the module, so a Cloud Run log line says which surface failed. Everything
#: this module discards from a failed token mint is written here first: inside the
#: container the metadata server is the *only* token path, and its failures used to leave
#: no trace at all.
_log = logging.getLogger(__name__)


class MCPTransportError(RuntimeError):
    """Anything that stopped this transport from getting an answer."""


class MCPProtocolError(MCPTransportError):
    """The server was reached and did not answer with a usable result.

    Covers an HTTP status, a JSON-RPC ``error`` object, and a body that is neither
    parseable JSON nor a usable SSE frame. One class because the caller's options are the
    same in all three cases, and the message always carries what actually came back.
    """


class IdentityTokenUnavailable(MCPTransportError):
    """Nothing here can mint a Cloud Run ID token for the MCP server.

    Raised *before* any request is sent. Sending an unauthenticated one instead would
    come back as Cloud Run's own 401 HTML page, which says nothing about credentials
    being missing and reads exactly like the service being broken.
    """


class TokenSource(Protocol):
    """Anything that can produce a bearer token. Injected so tests never mint one."""

    async def token(self) -> str: ...


# -- result shapes ---------------------------------------------------------------
#
# Plain dataclasses rather than the MCP SDK's pydantic models: what GrafanaMCP reads is a
# structural contract (``.content`` blocks with ``.text``/``.data``/``.mimeType``, and
# ``.isError``), its tests already fake exactly this shape, and matching it here keeps the
# wrapper working against both this transport and a real ``mcp.ClientSession``.


@dataclass(frozen=True)
class ContentBlock:
    """One block of an MCP tool result.

    ``mimeType`` keeps the server's camelCase spelling on purpose: it is the attribute
    :meth:`dailies_api.mcp_client.GrafanaMCP.get_panel_image` reads off an image block,
    and renaming it here would break that wrapper only at runtime, only against a real
    server, and only on the one tool that returns pixels.
    """

    type: str
    text: str | None = None
    data: str | None = None
    mimeType: str | None = None  # the wire spelling, read by mcp_client

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ContentBlock:
        return cls(
            type=str(payload.get("type", "")),
            text=payload.get("text"),
            data=payload.get("data"),
            mimeType=payload.get("mimeType"),
        )


@dataclass(frozen=True)
class ToolResult:
    """What ``tools/call`` answered.

    ``isError`` is the MCP spelling and it matters: a *failed tool* is reported inside a
    successful response, and :func:`dailies_api.mcp_client._check_ok` reads this exact
    attribute to turn it into a typed error rather than a JSON decode failure.
    """

    content: list[ContentBlock] = field(default_factory=list)
    isError: bool = False  # the wire spelling, read by mcp_client

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ToolResult:
        blocks = payload.get("content") or []
        return cls(
            content=[ContentBlock.from_payload(block) for block in blocks],
            isError=bool(payload.get("isError", False)),
        )


@dataclass(frozen=True)
class ToolDescriptor:
    """One entry of ``tools/list``. Only ``name`` is read today."""

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] | None = None  # the wire spelling


@dataclass(frozen=True)
class ToolListing:
    """What ``tools/list`` answered, with pagination already followed."""

    tools: list[ToolDescriptor] = field(default_factory=list)


# -- authentication --------------------------------------------------------------


def _token_expiry(token: str) -> float | None:
    """The ``exp`` claim, in unix seconds, or ``None`` if it cannot be read.

    The signature is not verified and must not be: this process is the token's *bearer*,
    not its audience, and the only thing it needs from the payload is when to ask for a
    new one. Cloud Run is the party that validates it.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    expiry = claims.get("exp") if isinstance(claims, dict) else None
    return float(expiry) if isinstance(expiry, (int, float)) else None


class IdentityTokenSource:
    """Mints and caches a Cloud Run ID token for one audience.

    The audience is the *target service's URL*, not this service's, and not a scope. A
    token minted for the wrong audience is rejected by Cloud Run with the same 401 as no
    token at all, which is why the audience is a constructor argument rather than
    something assembled at the call site.

    Order of sources, and the reason for it:

    1. **The metadata server.** Inside Cloud Run this is the only thing that can mint a
       token for an arbitrary audience, and it needs no configuration at all.
    2. **``GOOGLE_ID_TOKEN``.** Outside Cloud Run the metadata hostname does not resolve,
       which surfaces as a connection error rather than a 404. *That* verdict, and only
       that one, is remembered, so a laptop run does not pay for a failed DNS lookup on
       every token refresh; it cannot change without the process moving hosts.
    3. **Neither.** Raise :class:`IdentityTokenUnavailable` saying what the metadata
       server actually did, rather than sending an unauthenticated request.

    A timeout and a non-200 are deliberately *not* that verdict, and the difference is
    the whole point of the split. Inside the container the metadata path is the only one
    that can produce a token, so treating a slow moment or a 500 as "not on Cloud Run"
    both latches the wrong answer permanently and prints a sentence that is the exact
    opposite of the truth to the only person who can fix it. A 5xx and a timeout are
    retried :data:`METADATA_ATTEMPTS` times; a 4xx is not, because a 403 or a 404 is a
    decision about this service account or this audience and repeating it just repeats it.
    Whatever the last failure was, it is logged and carried into the raised message.

    The token is cached until :data:`REFRESH_MARGIN_SECONDS` before its ``exp``, so one
    investigation's worth of tool calls mints once. The fill is serialised under a lock
    because the agent issues its Grafana calls from concurrent tasks and two of them
    reaching an empty cache together would otherwise both mint.
    """

    def __init__(
        self,
        audience: str,
        *,
        client: httpx.AsyncClient,
        env: Mapping[str, str] | None = None,
        now: Callable[[], float] = time.time,
        refresh_margin_seconds: float = REFRESH_MARGIN_SECONDS,
        timeout_seconds: float = 5.0,
        attempts: int = METADATA_ATTEMPTS,
        retry_delay_seconds: float = METADATA_RETRY_DELAY_SECONDS,
    ) -> None:
        self.audience = audience
        self._client = client
        self._env = env
        self._now = now
        self._margin = refresh_margin_seconds
        self._timeout = timeout_seconds
        self._attempts = max(1, attempts)
        self._retry_delay = retry_delay_seconds
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._metadata_available = True
        #: What the metadata server last did wrong, as a sentence. The message raised
        #: when nothing can mint a token is built from this rather than assuming.
        self._metadata_error: str | None = None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        """A bearer token good for at least the refresh margin."""
        if self._token is not None and self._now() < self._expires_at - self._margin:
            return self._token
        async with self._lock:
            # Re-checked under the lock: whoever held it may have just refreshed.
            if self._token is not None and self._now() < self._expires_at - self._margin:
                return self._token
            token = await self._mint()
            expiry = _token_expiry(token)
            self._token = token
            self._expires_at = (
                expiry if expiry is not None else self._now() + FALLBACK_TTL_SECONDS + self._margin
            )
            return token

    async def _mint(self) -> str:
        if self._metadata_available:
            token = await self._from_metadata()
            if token is not None:
                return token
        token = self._from_env()
        if token is not None:
            return token
        raise IdentityTokenUnavailable(
            f"No Cloud Run ID token could be obtained for audience {self.audience!r}. "
            f"{self._metadata_verdict()} {ID_TOKEN_ENV} is not set either. Set it to a "
            "token for that audience, for example: gcloud auth print-identity-token "
            "--impersonate-service-account=<runtime sa> "
            f"--audiences={self.audience}"
        )

    def _metadata_verdict(self) -> str:
        """One sentence about what the metadata server did, never an assumption.

        The old fixed text claimed the host was unreachable "so this is not running on
        Cloud Run" whatever had actually happened, which inside the container is both
        false and the only diagnostic an operator gets.
        """
        if self._metadata_error is not None:
            return self._metadata_error
        return f"The metadata server at {METADATA_IDENTITY_URL} was not asked."

    async def _from_metadata(self) -> str | None:
        """A freshly minted token, or ``None`` with ``_metadata_error`` saying why."""
        last_error: str | None = None
        for attempt in range(1, self._attempts + 1):
            retryable = True
            try:
                response = await self._client.get(
                    METADATA_IDENTITY_URL,
                    params={"audience": self.audience, "format": "full"},
                    headers={"Metadata-Flavor": "Google"},
                    timeout=self._timeout,
                )
            except httpx.ConnectError as exc:
                # The one genuine verdict about the host: the metadata name does not
                # resolve, or nothing is listening on it, so this process is not on
                # Cloud Run. Latched, because that cannot change under a running process.
                self._metadata_available = False
                self._metadata_error = (
                    f"The metadata server at {METADATA_IDENTITY_URL} is not reachable "
                    f"(so this is not running on Cloud Run): {exc!r}."
                )
                _log.debug(
                    "No metadata server for audience %s; falling back to %s: %r",
                    self.audience,
                    ID_TOKEN_ENV,
                    exc,
                )
                return None
            except httpx.TimeoutException as exc:
                # NOT latched. A timeout is a transient, not a statement about the host:
                # a container under startup contention can time out against a metadata
                # server that answers a second later, and remembering it as "not on
                # Cloud Run" would leave the only working token path switched off for
                # the life of the process.
                last_error = (
                    f"The metadata server at {METADATA_IDENTITY_URL} timed out after "
                    f"{self._timeout}s ({type(exc).__name__})."
                )
            else:
                if response.status_code == 200:
                    token = response.text.strip()
                    if token:
                        return token
                    last_error = (
                        f"The metadata server at {METADATA_IDENTITY_URL} answered 200 "
                        "with an empty body."
                    )
                else:
                    last_error = (
                        f"The metadata server at {METADATA_IDENTITY_URL} answered "
                        f"{response.status_code} for audience {self.audience!r}: "
                        f"{response.text[:200]!r}."
                    )
                    # A 4xx is a decision about this service account or this audience.
                    # Retrying repeats it; only a 5xx is worth asking again.
                    retryable = response.status_code >= 500
            if retryable and attempt < self._attempts:
                _log.warning(
                    "Cloud Run ID token mint failed, retrying (attempt %d/%d): %s",
                    attempt,
                    self._attempts,
                    last_error,
                )
                if self._retry_delay > 0:
                    await asyncio.sleep(self._retry_delay)
                continue
            _log.warning("Cloud Run ID token mint failed: %s", last_error)
            break
        self._metadata_error = last_error
        return None

    def _from_env(self) -> str | None:
        import os

        env = os.environ if self._env is None else self._env
        return (env.get(ID_TOKEN_ENV) or "").strip() or None


# -- the session -----------------------------------------------------------------


def _endpoint(url: str) -> str:
    """The ``/mcp`` endpoint for a service URL, without doubling an existing one."""
    parts = urlsplit(url.rstrip("/"))
    path = parts.path
    if not path.endswith(MCP_ENDPOINT_PATH):
        path = f"{path}{MCP_ENDPOINT_PATH}"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _audience(url: str) -> str:
    """The Cloud Run *service* origin to mint an ID token for.

    Not the same string as :func:`_endpoint`, and the difference is load bearing. Cloud
    Run validates an ID token's ``aud`` against the service URL with no path on it, so a
    token minted for ``https://host/mcp`` is rejected with the same bare 401 as no token
    at all. :func:`connect` accepts a URL that already names ``/mcp``, so handing it
    through unchanged set that trap for whoever pastes the endpoint form into
    ``DAILIES_MCP_URL``.
    """
    parts = urlsplit(url.rstrip("/"))
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _sse_payloads(body: str) -> list[Any]:
    """Every JSON payload carried by the ``data:`` fields of an SSE body.

    Per the SSE grammar a single event may carry several ``data:`` lines, which are
    joined with newlines; an event is terminated by a blank line. Anything that is not
    parseable JSON is skipped rather than raised on, because the caller is looking for
    one specific message and a keep-alive comment must not fail the call.
    """
    payloads: list[Any] = []
    lines: list[str] = []

    def flush() -> None:
        if not lines:
            return
        try:
            payloads.append(json.loads("\n".join(lines)))
        except json.JSONDecodeError:
            pass
        lines.clear()

    for raw in body.splitlines():
        line = raw.rstrip("\r")
        if not line:
            flush()
            continue
        if line.startswith("data:"):
            lines.append(line[len("data:") :].lstrip())
    flush()
    return payloads


class StreamableHTTPSession:
    """One MCP session over Streamable HTTP, satisfying ``mcp_client.MCPSession``.

    Not reusable after :meth:`aclose`, and not a connection pool: a session is a
    negotiated conversation with one server, and the id that identifies it is torn down
    when the session ends. Use :func:`connect` rather than constructing and closing this
    by hand.

    ``call_tool`` takes its arguments positionally to match the protocol the wrapper
    declares, which is in turn what the MCP SDK's ``ClientSession`` declares.
    """

    def __init__(
        self,
        url: str,
        *,
        client: httpx.AsyncClient,
        token_source: TokenSource,
        owns_client: bool = False,
        timeout_seconds: float = 120.0,
    ) -> None:
        self.url = _endpoint(url)
        self._client = client
        self._tokens = token_source
        self._owns_client = owns_client
        self._timeout = timeout_seconds
        self._session_id: str | None = None
        self._protocol_version = PROTOCOL_VERSION
        self._initialized = False
        self._closed = False
        self._next_id = 0
        self._id_lock = asyncio.Lock()

    @property
    def session_id(self) -> str | None:
        """The server's ``Mcp-Session-Id``, or ``None`` before ``initialize``."""
        return self._session_id

    @property
    def closed(self) -> bool:
        return self._closed

    async def initialize(self) -> dict[str, Any]:
        """Negotiate the session: ``initialize`` then ``notifications/initialized``.

        Idempotent, so a caller that is unsure whether a session is open may call it
        without opening a second one.
        """
        if self._initialized:
            return {}
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        if isinstance(result, dict):
            self._protocol_version = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        # Marked initialized before the notification, because the notification itself is
        # a post-initialize request and must carry the session id and protocol header.
        self._initialized = True
        await self._notify("notifications/initialized")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> ToolListing:
        """Every tool the server exposes, following ``nextCursor`` to the end."""
        tools: list[ToolDescriptor] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else {}
            result = await self._rpc("tools/list", params)
            payload = result if isinstance(result, dict) else {}
            for entry in payload.get("tools") or []:
                tools.append(
                    ToolDescriptor(
                        name=str(entry.get("name", "")),
                        description=entry.get("description"),
                        inputSchema=entry.get("inputSchema"),
                    )
                )
            cursor = payload.get("nextCursor")
            if not cursor:
                return ToolListing(tools=tools)

    async def call_tool(self, name: str, args: dict[str, Any], /) -> ToolResult:
        """Run one tool. A tool that *failed* comes back as ``isError``, not a raise."""
        result = await self._rpc("tools/call", {"name": name, "arguments": args})
        return ToolResult.from_payload(result if isinstance(result, dict) else {})

    async def aclose(self) -> None:
        """End the session, and the HTTP client if this object opened it.

        The DELETE is best effort: the spec makes session termination optional and a
        server that answers 405 has simply not implemented it. Failing a close because
        the cleanup call was refused would turn a finished investigation into an error.
        """
        if self._closed:
            return
        self._closed = True
        if self._session_id is not None:
            try:
                headers = await self._authorized_headers()
                await self._client.delete(self.url, headers=headers, timeout=5.0)
            except (httpx.HTTPError, MCPTransportError):
                pass
        self._initialized = False
        if self._owns_client:
            await self._client.aclose()

    # -- the wire ----------------------------------------------------------------

    async def _rpc(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        if method != "initialize" and not self._initialized:
            raise MCPProtocolError(
                f"{method!r} was called on a session that has not completed its MCP "
                "initialize handshake. Use mcp_transport.connect(), or await "
                "session.initialize() first."
            )
        request_id = await self._request_id()
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params:
            body["params"] = dict(params)
        response = await self._post(body, capture_session_id=(method == "initialize"))
        return self._result_of(response, request_id, method)

    async def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        """Send a notification: no id, and no result to wait for."""
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            body["params"] = dict(params)
        await self._post(body, capture_session_id=False)

    async def _post(self, body: Mapping[str, Any], *, capture_session_id: bool) -> httpx.Response:
        # The token is fetched before the request is built, so a missing credential
        # raises IdentityTokenUnavailable here rather than reaching Cloud Run as an
        # unauthenticated request and coming back as an unexplained 401.
        headers = await self._authorized_headers()
        try:
            response = await self._client.post(
                self.url, json=dict(body), headers=headers, timeout=self._timeout
            )
        except httpx.HTTPError as exc:
            raise MCPProtocolError(
                f"{body.get('method')!r} could not reach the MCP server at {self.url}: {exc}"
            ) from exc
        if capture_session_id:
            # The response HEADER, not the body. Nothing in the JSON result carries it.
            self._session_id = response.headers.get("Mcp-Session-Id") or self._session_id
        if response.status_code >= 400:
            raise MCPProtocolError(
                f"The MCP server answered {response.status_code} to "
                f"{body.get('method')!r} at {self.url}: {response.text[:500]!r}"
            )
        return response

    async def _authorized_headers(self) -> dict[str, str]:
        headers = self._headers()
        headers["Authorization"] = f"Bearer {await self._tokens.token()}"
        return headers

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            # Both, always. The endpoint chooses which one it answers with per request,
            # and offering only JSON makes it refuse the ones it wants to stream.
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id is not None:
            headers["Mcp-Session-Id"] = self._session_id
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol_version
        return headers

    async def _request_id(self) -> int:
        async with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _result_of(self, response: httpx.Response, request_id: int, method: str) -> Any:
        for message in self._messages(response, method):
            if not isinstance(message, dict) or message.get("id") != request_id:
                # A notification, or an answer to a different request sharing the stream.
                continue
            if "error" in message:
                error = message["error"] or {}
                raise MCPProtocolError(
                    f"The MCP server refused {method!r}: "
                    f"{error.get('message', error)!r} (code {error.get('code')})"
                )
            return message.get("result")
        raise MCPProtocolError(
            f"The MCP server's answer to {method!r} carried no result for request "
            f"{request_id}: {response.text[:500]!r}"
        )

    @staticmethod
    def _messages(response: httpx.Response, method: str) -> list[Any]:
        content_type = response.headers.get("content-type", "")
        body = response.text
        if "text/event-stream" in content_type:
            return _sse_payloads(body)
        if not body.strip():
            return []
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            # An SSE body served without the header is likelier than a broken server, so
            # try the framing before giving up on it.
            payloads = _sse_payloads(body)
            if payloads:
                return payloads
            raise MCPProtocolError(
                f"The MCP server's answer to {method!r} was neither JSON nor an SSE "
                f"stream: {body[:500]!r}"
            ) from None
        return payload if isinstance(payload, list) else [payload]


@asynccontextmanager
async def connect(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    token_source: TokenSource | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[StreamableHTTPSession]:
    """Open an initialized MCP session against ``url`` and close it afterwards.

    Args:
        url: The MCP service URL. ``/mcp`` is appended if it is not already there.
        client: An HTTP client to use. Omit it and one is created and closed with the
            session; pass one to share a connection pool, or to test without a network.
        token_source: Where the bearer token comes from. Omit it for the Cloud Run
            identity token minted for ``url``'s service origin - the audience Cloud Run
            checks against, which is the URL with no ``/mcp`` path on it.
        env: Environment for the default token source's ``GOOGLE_ID_TOKEN`` fallback.
            Defaults to the process environment.
    """
    owns_client = client is None
    http = httpx.AsyncClient() if client is None else client
    tokens = (
        IdentityTokenSource(_audience(url), client=http, env=env)
        if token_source is None
        else token_source
    )
    session = StreamableHTTPSession(url, client=http, token_source=tokens, owns_client=owns_client)
    try:
        await session.initialize()
        yield session
    finally:
        await session.aclose()
