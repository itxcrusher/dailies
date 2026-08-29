"""A bad query the agent wrote must not end the investigation.

Driven against the deployed system on 2026-08-29: asked to diagnose SH050, the model
wrote LogQL that Loki rejected -

    parse error at line 0, col 52: syntax error: unexpected |=, expecting STRING or ip

and the whole investigation died with ToolCallFailed. One malformed query, written by
the agent itself, cost the entire diagnosis and returned a 502 to the board.

That is the wrong shape of failure. A rejected query is information the model can act
on: it wrote the query, so it can correct it, and handing the error back is what lets
it. Raising instead throws away the only thing that would have recovered the run.
"""

import json
from types import SimpleNamespace

import pytest
from dailies_api.agent import build_investigator
from dailies_api.mcp_client import GrafanaMCP


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Result:
    def __init__(self, content, is_error=False):
        self.content = content
        self.isError = is_error


class RejectingSession:
    """Rejects the first Loki query the way Loki rejects bad LogQL, then answers."""

    TOOLS = ("query_loki_logs", "query_prometheus", "list_prometheus_metric_names")

    def __init__(self):
        self.calls = 0

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in self.TOOLS])

    async def call_tool(self, name, args):
        self.calls += 1
        if self.calls == 1:
            return _Result(
                [
                    _Block(
                        text=(
                            "loki API returned status code 400: parse error at line 0, "
                            "col 52: syntax error: unexpected |=, expecting STRING or ip"
                        )
                    )
                ],
                is_error=True,
            )
        return _Result([_Block(text=json.dumps({"data": ["a log line"]}))])


def loki_tool(agent):
    for tool in agent.tools:
        if getattr(tool.func, "__name__", "") == "query_loki_logs":
            return tool.func
    raise AssertionError("query_loki_logs was not wrapped as a tool")


def investigator(session):
    return build_investigator(
        ["query_loki_logs"],
        grafana=GrafanaMCP(session=session, prometheus_uid="p", loki_uid="l"),
    )


@pytest.mark.asyncio
async def test_a_rejected_query_comes_back_to_the_model_instead_of_raising():
    tool = loki_tool(investigator(RejectingSession()))

    result = await tool(logql='{shot="SH050"} |= bad')

    assert isinstance(result, dict), "the model must receive a result it can read"
    assert "error" in result
    assert "parse error" in str(result["error"])


@pytest.mark.asyncio
async def test_the_model_can_then_succeed_on_a_corrected_query():
    session = RejectingSession()
    tool = loki_tool(investigator(session))

    await tool(logql='{shot="SH050"} |= bad')
    good = await tool(logql='{shot="SH050"} |= "asset"')

    assert good == {"data": ["a log line"]}
    assert session.calls == 2


@pytest.mark.asyncio
async def test_the_tool_still_presents_its_real_name_and_docs_to_the_model():
    """The wrapper must not flatten the surface ADK builds its schema from."""
    tool = loki_tool(investigator(RejectingSession()))

    assert tool.__name__ == "query_loki_logs"
    assert tool.__doc__, "the docstring is the model's description of the tool"
