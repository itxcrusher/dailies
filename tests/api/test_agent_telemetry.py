"""The agent measures everything except itself, until now.

A project whose whole argument is that you cannot trust what you cannot see, and which
asks a supervisor to act on a model's answer, should be able to say what that answer
cost, how long it took and whether it failed. It could not. The render pipeline was fully
instrumented and the investigator was a black box.

Grafana's own track materials name AI Observability as the one recommended enhancement,
for exactly this: the agent's LLM calls and token usage.

The metric and attribute names here are the OpenTelemetry GenAI semantic conventions,
spelled as literals rather than imported. `opentelemetry.semconv._incubating` is a
private path, and depending on one at runtime already cost this project an entire render
when `LogRecord` moved between SDK versions. The literals are a published standard and
will not move; the test below cross-checks them against the package when it is importable,
so a drift is caught in CI rather than in a container.
"""

import pytest
from dailies_api.agent_telemetry import (
    DURATION_METRIC,
    TOKEN_METRIC,
    AgentTelemetry,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader


class Usage:
    """Stands in for google.genai.types.GenerateContentResponseUsageMetadata."""

    def __init__(self, prompt=None, candidates=None, thoughts=None, total=None):
        self.prompt_token_count = prompt
        self.candidates_token_count = candidates
        self.thoughts_token_count = thoughts
        self.total_token_count = total


def collect(reader):
    data = reader.get_metrics_data()
    if data is None:
        return {}
    return {
        m.name: m for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics
    }


def make():
    reader = InMemoryMetricReader()
    return AgentTelemetry(MeterProvider(metric_readers=[reader])), reader


def points(metrics, name):
    return metrics[name].data.data_points


def test_input_and_output_tokens_are_separable():
    """One metric, split by gen_ai.token.type, is what the convention specifies."""
    tel, reader = make()

    tel.record_usage(Usage(prompt=100, candidates=25), model="gemini-2.5-flash")

    pts = points(collect(reader), TOKEN_METRIC)
    by_type = {p.attributes["gen_ai.token.type"]: p.sum for p in pts}
    assert by_type["input"] == 100
    assert by_type["output"] == 25


def test_thinking_tokens_are_reported_and_not_hidden_inside_output():
    """Gemini 2.5 bills thinking tokens, and they appear in neither prompt nor candidates.

    An agent whose cost is dominated by reasoning it never shows is exactly the thing a
    cost dashboard is for, so folding them into 'output' would hide the finding.
    """
    tel, reader = make()

    tel.record_usage(Usage(prompt=100, candidates=25, thoughts=400), model="gemini-2.5-flash")

    by_type = {
        p.attributes["gen_ai.token.type"]: p.sum for p in points(collect(reader), TOKEN_METRIC)
    }
    assert by_type["thinking"] == 400
    assert by_type["output"] == 25, "thinking must not be folded into output"


def test_a_missing_count_records_nothing_rather_than_a_zero():
    """A zero is a measurement. An absent count is not, and averaging it in would lie."""
    tel, reader = make()

    tel.record_usage(Usage(prompt=100), model="gemini-2.5-flash")

    by_type = {p.attributes["gen_ai.token.type"] for p in points(collect(reader), TOKEN_METRIC)}
    assert by_type == {"input"}


def test_every_token_point_carries_the_model_and_provider():
    tel, reader = make()

    tel.record_usage(Usage(prompt=10, candidates=2), model="gemini-2.5-flash")

    for p in points(collect(reader), TOKEN_METRIC):
        assert p.attributes["gen_ai.request.model"] == "gemini-2.5-flash"
        assert p.attributes["gen_ai.provider.name"] == "gcp.vertex_ai"
        assert p.attributes["gen_ai.operation.name"] == "invoke_agent"


def test_the_duration_of_an_investigation_is_recorded():
    tel, reader = make()

    tel.record_operation(12.5, model="gemini-2.5-flash")

    pt = points(collect(reader), DURATION_METRIC)[0]
    assert pt.sum == pytest.approx(12.5)
    assert pt.attributes["gen_ai.request.model"] == "gemini-2.5-flash"


def test_a_failed_investigation_is_recorded_with_its_error_type():
    """A dashboard showing only successful calls is how a broken agent looks healthy."""
    tel, reader = make()

    tel.record_operation(3.0, model="gemini-2.5-flash", error_type="_ResourceExhaustedError")

    pt = points(collect(reader), DURATION_METRIC)[0]
    assert pt.attributes["error.type"] == "_ResourceExhaustedError"


def test_a_successful_call_carries_no_error_attribute():
    """Present-and-empty would split the series and make the success rate unreadable."""
    tel, reader = make()

    tel.record_operation(3.0, model="gemini-2.5-flash")

    assert "error.type" not in points(collect(reader), DURATION_METRIC)[0].attributes


def test_recording_never_raises_on_a_degenerate_usage_object():
    """This runs inside the diagnose path; instrumentation must never fail the request."""
    tel, _ = make()

    tel.record_usage(None, model="gemini-2.5-flash")
    tel.record_usage(object(), model="gemini-2.5-flash")


def test_the_names_match_the_published_semantic_conventions():
    """Cross-check the literals against the semconv package, which lives on a private path.

    Depending on `opentelemetry.semconv._incubating` at runtime is what the literals
    avoid; checking against it here is free and catches a drift in CI.
    """
    semconv = pytest.importorskip("opentelemetry.semconv._incubating.attributes.gen_ai_attributes")

    assert TOKEN_METRIC == "gen_ai.client.token.usage"
    assert DURATION_METRIC == "gen_ai.client.operation.duration"
    assert semconv.GEN_AI_TOKEN_TYPE == "gen_ai.token.type"
    assert semconv.GEN_AI_REQUEST_MODEL == "gen_ai.request.model"
    assert semconv.GEN_AI_PROVIDER_NAME == "gen_ai.provider.name"
    assert semconv.GEN_AI_OPERATION_NAME == "gen_ai.operation.name"


# --- wired into the investigation ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_agent_records_what_the_investigation_cost():
    """The metrics must come from the real run path, not from a parallel code path.

    An instrumentation that only fires in its own test is the observability equivalent of
    a dashboard nobody reads.
    """
    from types import SimpleNamespace

    from dailies_api import investigation

    tel, reader = make()
    recorded = {}

    class FakeRunner:
        def __init__(self, agent, app_name):
            self.session_service = SimpleNamespace(
                create_session=lambda **kw: _coro(SimpleNamespace(id="s1"))
            )

        async def run_async(self, **kw):
            yield SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(text='{"ok":1}')]),
                usage_metadata=Usage(prompt=120, candidates=30, thoughts=200),
                is_final_response=lambda: True,
            )

        async def close(self):
            recorded["closed"] = True

    async def _coro(value):
        return value

    await investigation.run_agent(
        SimpleNamespace(model="gemini-2.5-flash"),
        "diagnose SH010",
        telemetry=tel,
        runner_factory=FakeRunner,
    )

    metrics = collect(reader)
    assert TOKEN_METRIC in metrics, "an investigation must report what it cost"
    assert DURATION_METRIC in metrics, "and how long it took"
    by_type = {p.attributes["gen_ai.token.type"]: p.sum for p in points(metrics, TOKEN_METRIC)}
    assert by_type == {"input": 120, "output": 30, "thinking": 200}


@pytest.mark.asyncio
async def test_a_failed_investigation_still_reports_its_duration():
    """The failing calls are the ones a cost and reliability dashboard most needs."""
    from types import SimpleNamespace

    from dailies_api import investigation

    tel, reader = make()

    class Exploding:
        def __init__(self, agent, app_name):
            self.session_service = SimpleNamespace(
                create_session=lambda **kw: _coro(SimpleNamespace(id="s1"))
            )

        async def run_async(self, **kw):
            raise ValueError("model said no")
            yield  # pragma: no cover - makes this an async generator

        async def close(self):
            pass

    async def _coro(value):
        return value

    with pytest.raises(ValueError):
        await investigation.run_agent(
            SimpleNamespace(model="gemini-2.5-flash"),
            "diagnose SH010",
            telemetry=tel,
            runner_factory=Exploding,
        )

    pt = points(collect(reader), DURATION_METRIC)[0]
    assert pt.attributes["error.type"] == "ValueError"
