"""A thin typed wrapper over the Grafana MCP server's tool surface.

Everything Dailies knows about a live render comes back through here: the agents read
Prometheus and Loki, mark the timeline with annotations, open and update an IRM incident,
and pull a panel image for the validation step. Calling the MCP session directly from
agent code would spread three problems across every call site, so they are solved once
here:

1. **Spelling.** The server takes camelCase JSON keys (``datasourceUid``, ``startRfc3339``,
   ``roomPrefix``) and its own tool names. A typo in either is not a type error - it is a
   runtime failure against a live stack, usually mid-demo. The mapping lives in one file
   with tests pinning it, and the names were read off grafana/mcp-grafana source on
   2026-08-27 rather than guessed, as AGENTS.md requires.
2. **Failure shape.** An MCP tool reports failure *in a successful response*: ``isError``
   with the message as plain text. Feeding that to ``json.loads`` produces
   ``JSONDecodeError: Expecting value: line 1 column 1``, which names neither the tool nor
   the actual problem. Every failure here raises a ``GrafanaMCPError`` carrying both.
3. **Capability.** Not every Grafana stack has every tool - the IRM tools are absent
   unless Incident Response is provisioned, and the image renderer is a separate service.
   ``available_tools()`` / ``has_tool()`` let a caller degrade (annotate instead of opening
   an incident) rather than discover this by exception.

Datasource UIDs are never constants here. They differ per stack, so they are constructor
configuration with a per-call override; a UID baked into this file would make the wrapper
work on exactly one Grafana.
"""

from __future__ import annotations

import asyncio
import base64
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol
from urllib.parse import urlparse

from .windows import LOOKBACK

__all__ = [
    "GrafanaMCP",
    "GrafanaMCPError",
    "MCPSession",
    "MalformedToolResponse",
    "PanelImage",
    "ToolCallFailed",
]

# How much of a raw tool response goes into an exception message. The full text stays on
# `.raw`; this bound exists because these messages land in logs and in agent prompts, and
# a 2 MB HTML error page from a proxy would swamp both.
_MESSAGE_SNIPPET = 400


class GrafanaMCPError(Exception):
    """Base for every failure this wrapper raises.

    Always carries the tool that failed and the raw text the server sent, because "which
    call broke and what did Grafana actually say" is the whole of the first debugging step
    and neither survives a bare ``JSONDecodeError``.

    Every constructor argument is passed straight up to ``Exception``, so ``args`` matches
    the signature and the error can be serialised or copied. That is not theoretical: a
    process pool, a task queue, or a ``deepcopy`` inside a retry decorator rebuilds an
    exception from ``args``, and a mismatch there turns this typed error back into the
    opaque failure it exists to replace.
    """

    #: What went wrong, in words, for the message. Each subclass fills it in.
    detail = "failed"

    def __init__(self, tool: str, raw: str, *rest: Any) -> None:
        super().__init__(tool, raw, *rest)
        self.tool = tool
        self.raw = raw

    def __str__(self) -> str:
        # Rendered here rather than at construction, so ``args`` can stay the
        # constructor's own arguments.
        snippet = self.raw[:_MESSAGE_SNIPPET]
        if len(self.raw) > _MESSAGE_SNIPPET:
            snippet += f"... [{len(self.raw)} chars total, full text on .raw]"
        return f"Grafana MCP tool {self.tool!r} {self.detail}: {snippet!r}"


class ToolCallFailed(GrafanaMCPError):
    """The server ran the tool and the tool reported failure.

    The transport worked; Grafana said no. Typical causes are a wrong datasource UID, a
    PromQL/LogQL syntax error, a missing permission on the service-account token, or a
    subsystem that is not installed (the image renderer, IRM). Retrying the identical call
    is rarely the fix.
    """

    detail = "reported an error"


class MalformedToolResponse(GrafanaMCPError):
    """The tool claimed success but the response was not the shape it should be.

    An empty content list, no text block where JSON was expected, text that is not JSON,
    or a panel image with no image block. Usually something in the middle answered instead
    of Grafana - a proxy error page, an auth redirect - so the raw text is the evidence
    worth reading.

    ``reason`` names which of those four it was, so a caller can branch on the condition
    instead of substring-matching English: a response carrying no content at all is worth
    one retry, a proxy error page is not. The prose is for humans only.
    """

    NO_TEXT = "no_text"
    NOT_JSON = "not_json"
    NO_IMAGE = "no_image"
    BAD_BASE64 = "bad_base64"

    _DETAILS: ClassVar[dict[str, str]] = {
        NO_TEXT: "returned no text content",
        NOT_JSON: "returned content that is not JSON",
        NO_IMAGE: "returned no image block",
        BAD_BASE64: "returned an image block that is not valid base64",
    }

    def __init__(self, tool: str, raw: str, reason: str) -> None:
        super().__init__(tool, raw, reason)
        self.reason = reason

    @property
    def detail(self) -> str:
        return self._DETAILS.get(self.reason, self.reason)


@dataclass(frozen=True)
class PanelImage:
    """A rendered Grafana panel.

    ``get_panel_image`` is the one tool here that does not answer with JSON: it returns an
    MCP image block (base64 PNG) and, when it can build one, a deeplink text block. Bytes
    rather than the base64 string because every consumer - writing a file, attaching to a
    Gemini prompt, serving over HTTP - wants bytes, and re-decoding at each of those is
    three chances to get it wrong.
    """

    png: bytes
    mime_type: str
    deeplink: str | None = None


class MCPSession(Protocol):
    """The slice of an MCP client session this wrapper uses.

    Structural, so ``mcp.ClientSession`` satisfies it without importing the SDK here and
    a test fake satisfies it without subclassing anything. That is what keeps these tests
    off a live Grafana.

    The parameters of ``call_tool`` are positional-only, and the claim above is why. A
    protocol method's parameter *names* are part of the contract, and the SDK spells the
    second one ``arguments``; declaring it as ``args`` makes ``ClientSession`` fail the
    structural check under a strict type checker even though every call here is
    positional. So the calls stay positional and the protocol says so.
    """

    async def list_tools(self) -> Any: ...

    async def call_tool(self, name: str, args: dict[str, Any], /) -> Any: ...


def _args(**kwargs: Any) -> dict[str, Any]:
    """Drop unset arguments instead of sending them as null.

    The server's optional fields are ``omitempty``; an explicit ``null`` is not the same
    as absent, and sending one can override a server-side default with nothing.
    """
    return {key: value for key, value in kwargs.items() if value is not None}


def _first_text(result: Any) -> str | None:
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return text
    return None


def _describe_content(result: Any) -> str:
    """What *did* come back, for the errors where no usable block did.

    The base class promises the raw text the server sent; when there is none, the block
    types are the next best evidence. An empty content list, a lone image block and an
    embedded resource are three different problems and are otherwise indistinguishable
    in the message.
    """
    content = getattr(result, "content", None) or []
    return repr([getattr(block, "type", None) or type(block).__name__ for block in content])


def _deeplink(result: Any) -> str | None:
    """The panel deeplink, if the server sent one.

    Only a text block holding an http(s) URL counts. The rendering tool puts the image
    block first and the deeplink second today, so "first block with text" happens to be
    right, but the rule that encodes is "any text is the deeplink": one appended warning
    or truncation notice would become a broken link in every consumer, with no error
    anywhere. A missing deeplink is already the documented normal case, so anything
    unrecognised is treated as missing.
    """
    for block in getattr(result, "content", None) or []:
        if getattr(block, "type", None) != "text":
            continue
        text = (getattr(block, "text", None) or "").strip()
        parsed = urlparse(text)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return text
    return None


def _check_ok(tool: str, result: Any) -> None:
    if getattr(result, "isError", False):
        raise ToolCallFailed(tool, _first_text(result) or "")


class GrafanaMCP:
    """Calls Grafana MCP tools and hands back parsed results.

    The session is injected rather than built here: connecting is the caller's lifecycle
    concern (stdio vs streamable HTTP, auth, reconnects), and injecting it is what lets
    every test above run without a Grafana.

    The only state held is the cached tool list, and its fill is serialised with an
    ``asyncio.Lock`` because one instance is shared by concurrent agent tasks. Nothing
    here is thread-safe beyond what the underlying session is.
    """

    def __init__(
        self,
        session: MCPSession,
        *,
        prometheus_uid: str | None = None,
        loki_uid: str | None = None,
    ) -> None:
        self.session = session
        self.prometheus_uid = prometheus_uid
        self.loki_uid = loki_uid
        self._tool_names: list[str] | None = None
        self._tool_names_lock = asyncio.Lock()

    # -- capability discovery ----------------------------------------------------

    async def available_tools(self, *, refresh: bool = False) -> list[str]:
        """Tool names the connected server actually exposes.

        Cached after the first call: the set only changes when the server restarts with a
        different configuration, and re-listing on every check would put a round trip in
        front of every guarded call. Pass ``refresh=True`` after a reconnect.

        A copy, so a caller sorting or filtering the result in place cannot quietly
        corrupt the cache every later ``has_tool`` reads.

        The fill is serialised and re-checked under the lock: two agent tasks reaching
        their first ``has_tool`` together would otherwise both see an empty cache and
        both pay for a ``list_tools`` round trip.
        """
        if self._tool_names is not None and not refresh:
            return list(self._tool_names)
        async with self._tool_names_lock:
            if self._tool_names is None or refresh:
                listing = await self.session.list_tools()
                self._tool_names = [tool.name for tool in listing.tools]
            return list(self._tool_names)

    async def has_tool(self, name: str) -> bool:
        """Whether one tool is present, for callers that can degrade without it."""
        return name in await self.available_tools()

    # -- reading telemetry -------------------------------------------------------

    async def query_prometheus(
        self,
        expr: str,
        *,
        datasource_uid: str | None = None,
        start_time: str | None = None,
        end_time: str = "now",
        step_seconds: int | None = None,
        query_type: str | None = None,
    ) -> Any:
        """Run a PromQL query.

        ``end_time`` defaults to ``"now"`` because the server requires it and every
        Dailies query is about the render happening right now. Times are the server's own
        format: RFC3339, or relative (``"now-1.5h"``). Omitting ``start_time`` gives an
        instant query; supplying it with ``query_type="range"`` gives a range query, and
        that is the one that needs ``step_seconds``.
        """
        return await self._call_json(
            "query_prometheus",
            _args(
                datasourceUid=self._prometheus(datasource_uid),
                expr=expr,
                startTime=start_time,
                endTime=end_time,
                stepSeconds=step_seconds,
                queryType=query_type,
            ),
        )

    async def list_prometheus_metric_names(
        self,
        *,
        regex: str | None = None,
        datasource_uid: str | None = None,
        limit: int | None = None,
        page: int | None = None,
    ) -> Any:
        """Discover metric names on the datasource.

        The reason an agent does not need this project's metric names hardcoded in its
        prompt: it can ask what exists, filtered by ``regex`` (``"render_.*"``), and build
        the query from that. Cheap insurance against a metric rename silently producing
        empty graphs.
        """
        return await self._call_json(
            "list_prometheus_metric_names",
            _args(
                datasourceUid=self._prometheus(datasource_uid),
                regex=regex,
                limit=limit,
                page=page,
            ),
        )

    async def query_loki_logs(
        self,
        logql: str,
        *,
        datasource_uid: str | None = None,
        start_rfc3339: str | None = None,
        end_rfc3339: str | None = None,
        limit: int | None = None,
        direction: str | None = None,
    ) -> Any:
        """Run a LogQL query.

        Where the diagnosis agent gets its evidence: metrics say a shot is failing, the
        Blender stderr lines say why. ``direction="backward"`` (the server default) is
        newest-first, which is what you want when chasing a failure that just happened.

        **The time window defaults to the one the board lists shots over**, and that
        default is load-bearing rather than a convenience. Driven on the deployed system:
        asked about a sixteen-hour-old shot, the investigator ran exactly the right
        selector, omitted the times, and reported "no log entries" for a shot whose
        asset-missing warning was sitting in Loki. It called a broken shot clean.

        Instructing the model harder did not fix it and would not: these parameters are
        named for RFC3339 timestamps, the model does not know what time it is, and a
        parameter demanding an absolute instant is one it leaves out. The default belongs
        here, where the agent cannot get it wrong. An explicit window still wins, because
        an agent that did think about the range meant it.
        """
        return await self._call_json(
            "query_loki_logs",
            _args(
                datasourceUid=self._loki(datasource_uid),
                logql=logql,
                startRfc3339=start_rfc3339 if start_rfc3339 is not None else LOOKBACK,
                endRfc3339=end_rfc3339 if end_rfc3339 is not None else "now",
                limit=limit,
                direction=direction,
            ),
        )

    # -- writing back ------------------------------------------------------------

    async def create_annotation(
        self,
        *,
        text: str,
        dashboard_uid: str | None = None,
        panel_id: int | None = None,
        time_ms: int | None = None,
        time_end_ms: int | None = None,
        tags: list[str] | None = None,
    ) -> Any:
        """Mark the Grafana timeline with something an agent concluded.

        The cheap write path, and the one that works on every stack: a prediction or a
        diagnosis lands next to the metrics that produced it, so a human scrubbing the
        dashboard sees the reasoning in place.

        ``time_ms``/``time_end_ms`` are epoch **milliseconds**, Grafana's unit. Named with
        the unit because passing seconds here puts the annotation in 1970 and the call
        still succeeds, which is a silent wrong answer rather than an error.
        """
        return await self._call_json(
            "create_annotation",
            _args(
                text=text,
                dashboardUid=dashboard_uid,
                panelId=panel_id,
                time=time_ms,
                timeEnd=time_end_ms,
                tags=tags,
            ),
        )

    async def create_incident(
        self,
        *,
        title: str,
        severity: str,
        room_prefix: str,
        status: str | None = None,
        is_drill: bool | None = None,
        attach_url: str | None = None,
        attach_caption: str | None = None,
        labels: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Open a Grafana IRM incident.

        The escalation path for a shot that will not make the deadline. ``title``,
        ``severity`` and ``room_prefix`` are all required by the server. Guard this with
        ``has_tool("create_incident")``: the IRM tools are missing entirely on a stack
        without Incident Response, and annotation is the fallback.

        ``is_drill=True`` marks a rehearsal, which is what a demo run should be sending.
        """
        return await self._call_json(
            "create_incident",
            _args(
                title=title,
                severity=severity,
                roomPrefix=room_prefix,
                status=status,
                isDrill=is_drill,
                attachUrl=attach_url,
                attachCaption=attach_caption,
                labels=labels,
            ),
        )

    async def add_activity_to_incident(
        self,
        incident_id: str,
        body: str,
        *,
        event_time: str | None = None,
    ) -> Any:
        """Append a note to an incident's timeline.

        How the recovery loop stays auditable: every action an agent takes on an open
        incident is written back to the timeline a human is reading. ``event_time`` is
        RFC3339 and defaults server-side to now; pass it when logging something that
        happened earlier than the call.
        """
        return await self._call_json(
            "add_activity_to_incident",
            _args(incidentId=incident_id, body=body, eventTime=event_time),
        )

    # -- pixels ------------------------------------------------------------------

    async def get_panel_image(
        self,
        *,
        dashboard_uid: str,
        panel_id: int | None = None,
        org_id: int | None = None,
        width: int | None = None,
        height: int | None = None,
        theme: str | None = None,
        scale: int | None = None,
        timeout: int | None = None,
        time_range: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> PanelImage:
        """Render a panel (or the whole dashboard, if ``panel_id`` is omitted) as a PNG.

        Returns ``PanelImage``, not parsed JSON, because this tool answers with an MCP
        image block. Needs the Grafana Image Renderer service on the stack; without it the
        tool errors and this raises ``ToolCallFailed`` saying so.
        """
        tool = "get_panel_image"
        result = await self.session.call_tool(
            tool,
            _args(
                dashboardUid=dashboard_uid,
                panelId=panel_id,
                orgId=org_id,
                width=width,
                height=height,
                theme=theme,
                scale=scale,
                timeout=timeout,
                timeRange=time_range,
                variables=variables,
            ),
        )
        _check_ok(tool, result)

        for block in getattr(result, "content", None) or []:
            data = getattr(block, "data", None)
            if data is None:
                continue
            try:
                # binascii.Error is a ValueError subclass, so this catches both.
                png = base64.b64decode(data, validate=True)
            except ValueError as exc:
                raise MalformedToolResponse(
                    tool, str(data), MalformedToolResponse.BAD_BASE64
                ) from exc
            return PanelImage(
                png=png,
                mime_type=getattr(block, "mimeType", None) or "image/png",
                # The deeplink is best-effort on the server side and omitted when an
                # explicit org was requested, so its absence is normal, not an error.
                deeplink=_deeplink(result),
            )

        raise MalformedToolResponse(
            tool,
            _first_text(result) or _describe_content(result),
            MalformedToolResponse.NO_IMAGE,
        )

    # -- internals ---------------------------------------------------------------

    def _prometheus(self, override: str | None) -> str:
        return self._datasource(override, self.prometheus_uid, "prometheus_uid")

    def _loki(self, override: str | None) -> str:
        return self._datasource(override, self.loki_uid, "loki_uid")

    @staticmethod
    def _datasource(override: str | None, configured: str | None, field: str) -> str:
        """Resolve a datasource UID, per-call argument winning over the constructor.

        Raises before the round trip rather than letting the server answer "datasource not
        found" for a UID of ``None``, which reads like a Grafana problem instead of a
        missing configuration value on our side.
        """
        uid = override or configured
        if not uid:
            raise ValueError(
                f"No datasource UID: pass datasource_uid=, or set {field} on GrafanaMCP. "
                "UIDs differ per Grafana stack, so this client never assumes one."
            )
        return uid

    async def _call_json(self, tool: str, args: dict[str, Any]) -> Any:
        """Call a tool and parse the JSON the server puts in its first text block."""
        result = await self.session.call_tool(tool, args)
        _check_ok(tool, result)

        text = _first_text(result)
        if text is None:
            raise MalformedToolResponse(
                tool, _describe_content(result), MalformedToolResponse.NO_TEXT
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MalformedToolResponse(tool, text, MalformedToolResponse.NOT_JSON) from exc
