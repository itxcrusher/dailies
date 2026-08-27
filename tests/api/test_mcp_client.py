"""Tests for the Grafana MCP client wrapper.

The session is injected everywhere, so none of this needs a live Grafana. What is being
tested is the mapping this wrapper owns: our argument names onto the server's tool names
and JSON keys, and its failure modes onto typed errors. The tool names and key spellings
were verified against grafana/mcp-grafana source on 2026-08-27, so a drift in either
should break these tests loudly rather than at runtime against a real stack.
"""

import base64
import json
from types import SimpleNamespace

import pytest
from dailies_api.mcp_client import (
    GrafanaMCP,
    GrafanaMCPError,
    MalformedToolResponse,
    PanelImage,
    ToolCallFailed,
)

ALL_TOOLS = [
    "query_prometheus",
    "query_loki_logs",
    "create_annotation",
    "create_incident",
    "add_activity_to_incident",
    "list_prometheus_metric_names",
    "get_panel_image",
]


class _Block:
    """One MCP content block. Only the fields the wrapper reads are modelled."""

    def __init__(self, *, text=None, data=None, mime_type=None, type="text"):
        self.type = type
        if text is not None:
            self.text = text
        if data is not None:
            self.data = data
        if mime_type is not None:
            self.mimeType = mime_type


class _Result:
    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error


class FakeSession:
    """Records calls and replays canned results, keyed by tool name."""

    def __init__(self, results=None, tools=ALL_TOOLS):
        self.calls = []
        self._results = results or {}
        self._tools = tools

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self._tools])

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return self._results.get(name, _Result([_Block(text='{"data":{"result":[]}}')]))


def json_result(payload):
    return _Result([_Block(text=json.dumps(payload))])


# --- tool routing and argument mapping ------------------------------------------


async def test_query_prometheus_calls_the_right_tool():
    mcp = GrafanaMCP(session=FakeSession(), prometheus_uid="prom-uid")
    await mcp.query_prometheus("rate(render_job_frames_failed[5m])")
    assert mcp.session.calls[0][0] == "query_prometheus"


async def test_query_prometheus_sends_the_server_argument_spelling():
    mcp = GrafanaMCP(session=FakeSession(), prometheus_uid="prom-uid")
    await mcp.query_prometheus(
        "up", start_time="now-1h", end_time="now", step_seconds=30, query_type="range"
    )
    _, args = mcp.session.calls[0]
    assert args == {
        "datasourceUid": "prom-uid",
        "expr": "up",
        "startTime": "now-1h",
        "endTime": "now",
        "stepSeconds": 30,
        "queryType": "range",
    }


async def test_optional_arguments_are_omitted_rather_than_sent_as_null():
    mcp = GrafanaMCP(session=FakeSession(), prometheus_uid="prom-uid")
    await mcp.query_prometheus("up")
    _, args = mcp.session.calls[0]
    assert set(args) == {"datasourceUid", "expr", "endTime"}
    assert args["endTime"] == "now"


async def test_query_prometheus_returns_the_parsed_payload():
    session = FakeSession({"query_prometheus": json_result({"data": {"result": [1]}})})
    mcp = GrafanaMCP(session=session, prometheus_uid="prom-uid")
    assert await mcp.query_prometheus("up") == {"data": {"result": [1]}}


async def test_query_loki_logs_maps_onto_logql_and_rfc3339_keys():
    mcp = GrafanaMCP(session=FakeSession(), loki_uid="loki-uid")
    await mcp.query_loki_logs(
        '{job="render"} |= "error"',
        start_rfc3339="now-30m",
        end_rfc3339="now",
        limit=50,
        direction="backward",
    )
    assert mcp.session.calls[0] == (
        "query_loki_logs",
        {
            "datasourceUid": "loki-uid",
            "logql": '{job="render"} |= "error"',
            "startRfc3339": "now-30m",
            "endRfc3339": "now",
            "limit": 50,
            "direction": "backward",
        },
    )


async def test_list_prometheus_metric_names_is_a_discovery_call():
    mcp = GrafanaMCP(session=FakeSession(), prometheus_uid="prom-uid")
    await mcp.list_prometheus_metric_names(regex="render_.*", limit=100)
    assert mcp.session.calls[0] == (
        "list_prometheus_metric_names",
        {"datasourceUid": "prom-uid", "regex": "render_.*", "limit": 100},
    )


async def test_create_annotation_maps_onto_camel_case_keys():
    mcp = GrafanaMCP(session=FakeSession())
    await mcp.create_annotation(
        text="shot sq010_sh020 flagged at risk",
        dashboard_uid="dash-uid",
        panel_id=4,
        time_ms=1_724_000_000_000,
        tags=["dailies", "at-risk"],
    )
    assert mcp.session.calls[0] == (
        "create_annotation",
        {
            "text": "shot sq010_sh020 flagged at risk",
            "dashboardUid": "dash-uid",
            "panelId": 4,
            "time": 1_724_000_000_000,
            "tags": ["dailies", "at-risk"],
        },
    )


async def test_create_incident_sends_the_three_required_irm_fields():
    mcp = GrafanaMCP(session=FakeSession())
    await mcp.create_incident(
        title="sq010 will miss dailies",
        severity="minor",
        room_prefix="dailies",
        is_drill=True,
    )
    assert mcp.session.calls[0] == (
        "create_incident",
        {
            "title": "sq010 will miss dailies",
            "severity": "minor",
            "roomPrefix": "dailies",
            "isDrill": True,
        },
    )


async def test_add_activity_to_incident_maps_incident_id():
    mcp = GrafanaMCP(session=FakeSession())
    await mcp.add_activity_to_incident("inc-7", "retried 12 failed frames")
    assert mcp.session.calls[0] == (
        "add_activity_to_incident",
        {"incidentId": "inc-7", "body": "retried 12 failed frames"},
    )


# --- datasource UIDs are configuration, never constants --------------------------


async def test_method_argument_overrides_the_constructor_datasource():
    mcp = GrafanaMCP(session=FakeSession(), prometheus_uid="default-uid")
    await mcp.query_prometheus("up", datasource_uid="other-uid")
    assert mcp.session.calls[0][1]["datasourceUid"] == "other-uid"


async def test_a_prometheus_call_without_any_uid_says_so_before_calling():
    mcp = GrafanaMCP(session=FakeSession())
    with pytest.raises(ValueError, match="prometheus_uid"):
        await mcp.query_prometheus("up")
    assert mcp.session.calls == []


async def test_a_loki_call_without_any_uid_says_so_before_calling():
    mcp = GrafanaMCP(session=FakeSession())
    with pytest.raises(ValueError, match="loki_uid"):
        await mcp.query_loki_logs('{job="render"}')
    assert mcp.session.calls == []


# --- capability discovery --------------------------------------------------------


async def test_available_tools_returns_the_live_tool_names():
    mcp = GrafanaMCP(session=FakeSession(tools=["query_prometheus", "create_annotation"]))
    assert await mcp.available_tools() == ["query_prometheus", "create_annotation"]


async def test_available_tools_is_cached_until_refreshed():
    session = FakeSession(tools=["query_prometheus"])
    mcp = GrafanaMCP(session=session)
    await mcp.available_tools()
    session._tools = ["query_prometheus", "create_incident"]
    assert await mcp.available_tools() == ["query_prometheus"]
    assert await mcp.available_tools(refresh=True) == ["query_prometheus", "create_incident"]


async def test_available_tools_hands_back_a_copy_of_the_cache():
    mcp = GrafanaMCP(session=FakeSession(tools=["query_prometheus", "create_incident"]))
    (await mcp.available_tools()).clear()
    assert await mcp.has_tool("create_incident") is True


async def test_has_tool_lets_a_caller_degrade_when_irm_is_absent():
    # The IRM tools are absent on a Grafana stack without Incident Response provisioned.
    mcp = GrafanaMCP(session=FakeSession(tools=["query_prometheus", "create_annotation"]))
    assert await mcp.has_tool("create_annotation") is True
    assert await mcp.has_tool("create_incident") is False


# --- failure modes ---------------------------------------------------------------


async def test_a_tool_error_result_raises_a_typed_error_naming_the_tool():
    session = FakeSession(
        {"query_prometheus": _Result([_Block(text="datasource not found")], is_error=True)}
    )
    mcp = GrafanaMCP(session=session, prometheus_uid="prom-uid")
    with pytest.raises(ToolCallFailed) as caught:
        await mcp.query_prometheus("up")
    assert caught.value.tool == "query_prometheus"
    assert caught.value.raw == "datasource not found"
    assert "query_prometheus" in str(caught.value)
    assert "datasource not found" in str(caught.value)


async def test_non_json_content_raises_a_clear_error_not_a_jsondecodeerror():
    session = FakeSession(
        {"query_prometheus": _Result([_Block(text="<html>502 Bad Gateway</html>")])}
    )
    mcp = GrafanaMCP(session=session, prometheus_uid="prom-uid")
    with pytest.raises(MalformedToolResponse) as caught:
        await mcp.query_prometheus("up")
    assert caught.value.tool == "query_prometheus"
    assert caught.value.raw == "<html>502 Bad Gateway</html>"
    assert not isinstance(caught.value, json.JSONDecodeError)


async def test_a_result_with_no_text_block_raises_a_clear_error():
    session = FakeSession({"query_prometheus": _Result([])})
    mcp = GrafanaMCP(session=session, prometheus_uid="prom-uid")
    with pytest.raises(MalformedToolResponse) as caught:
        await mcp.query_prometheus("up")
    assert caught.value.tool == "query_prometheus"


async def test_every_typed_error_is_catchable_as_one_base_class():
    assert issubclass(ToolCallFailed, GrafanaMCPError)
    assert issubclass(MalformedToolResponse, GrafanaMCPError)


async def test_a_huge_error_body_is_truncated_in_the_message_but_kept_on_the_error():
    body = "x" * 5000
    session = FakeSession({"query_prometheus": _Result([_Block(text=body)], is_error=True)})
    mcp = GrafanaMCP(session=session, prometheus_uid="prom-uid")
    with pytest.raises(ToolCallFailed) as caught:
        await mcp.query_prometheus("up")
    assert caught.value.raw == body
    assert len(str(caught.value)) < 1000


# --- get_panel_image is the one tool that does not answer with JSON --------------


async def test_get_panel_image_returns_png_bytes_and_the_deeplink():
    png = b"\x89PNG\r\n\x1a\nfake"
    session = FakeSession(
        {
            "get_panel_image": _Result(
                [
                    _Block(
                        data=base64.b64encode(png).decode(),
                        mime_type="image/png",
                        type="image",
                    ),
                    _Block(text="https://grafana.example/d/dash-uid?viewPanel=4"),
                ]
            )
        }
    )
    mcp = GrafanaMCP(session=session)
    image = await mcp.get_panel_image(dashboard_uid="dash-uid", panel_id=4, width=800)
    assert isinstance(image, PanelImage)
    assert image.png == png
    assert image.mime_type == "image/png"
    assert image.deeplink == "https://grafana.example/d/dash-uid?viewPanel=4"
    assert mcp.session.calls[0] == (
        "get_panel_image",
        {"dashboardUid": "dash-uid", "panelId": 4, "width": 800},
    )


async def test_get_panel_image_without_an_image_block_raises_a_clear_error():
    session = FakeSession({"get_panel_image": _Result([_Block(text="renderer not installed")])})
    mcp = GrafanaMCP(session=session)
    with pytest.raises(MalformedToolResponse) as caught:
        await mcp.get_panel_image(dashboard_uid="dash-uid")
    assert caught.value.tool == "get_panel_image"


async def test_get_panel_image_surfaces_a_tool_error():
    session = FakeSession(
        {
            "get_panel_image": _Result(
                [_Block(text="image renderer not available")], is_error=True
            )
        }
    )
    mcp = GrafanaMCP(session=session)
    with pytest.raises(ToolCallFailed):
        await mcp.get_panel_image(dashboard_uid="dash-uid")
