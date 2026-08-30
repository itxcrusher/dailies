"""What the investigator itself costs, in OpenTelemetry GenAI semantic conventions.

The render pipeline was fully instrumented and the agent reading it was a black box. A
project arguing that you cannot trust what you cannot see, which asks a supervisor to act
on a model's answer, ought to be able to say what that answer cost, how long it took, and
whether it failed. Grafana's own track materials name AI Observability as the one
recommended enhancement, for exactly this.

**The names are literals, not imports, and that is deliberate.** The constants live in
``opentelemetry.semconv._incubating``, a private path, and depending on a private path at
runtime already cost this project an entire render when ``LogRecord`` moved between SDK
versions and the container died on an import that worked locally. The GenAI conventions
are a published standard and these strings will not move; the test cross-checks them
against the package when it is importable, so a drift is caught in CI rather than in a
container at 2am.
"""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry.metrics import MeterProvider

__all__ = ["DURATION_METRIC", "TOKEN_METRIC", "AgentTelemetry"]

_log = logging.getLogger(__name__)

#: Histogram of token consumption, split by ``gen_ai.token.type``.
TOKEN_METRIC = "gen_ai.client.token.usage"

#: Histogram of how long one agent invocation took, in seconds.
DURATION_METRIC = "gen_ai.client.operation.duration"

#: This project runs Gemini through Vertex, which the conventions spell exactly this way.
_PROVIDER = "gcp.vertex_ai"

#: The whole investigation is one agent invocation, not a single chat completion: it runs
#: several model turns with tool calls between them, and ``invoke_agent`` is the convention's
#: name for that shape.
_OPERATION = "invoke_agent"

#: Which usage field maps to which token type.
#:
#: ``thoughts_token_count`` is listed separately rather than folded into output, and the
#: distinction is the point of measuring at all. Gemini 2.5 bills thinking tokens, they
#: appear in neither the prompt nor the candidates count, and an agent whose cost is
#: dominated by reasoning it never shows is precisely what a cost dashboard exists to
#: reveal. Folding them into "output" would hide the finding inside the answer.
_TOKEN_FIELDS: dict[str, str] = {
    "prompt_token_count": "input",
    "candidates_token_count": "output",
    "thoughts_token_count": "thinking",
}


class AgentTelemetry:
    """Records what one investigation cost onto OTLP instruments."""

    def __init__(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter("dailies.agent")
        self._tokens = meter.create_histogram(
            TOKEN_METRIC,
            unit="{token}",
            description="Tokens consumed by one investigator invocation",
        )
        self._duration = meter.create_histogram(
            DURATION_METRIC,
            unit="s",
            description="Wall-clock time of one investigator invocation",
        )

    def record_usage(self, usage: Any, *, model: str) -> None:
        """Record the token counts from an ADK event's ``usage_metadata``.

        Args:
            usage: A ``GenerateContentResponseUsageMetadata``, or anything at all. This
                runs inside the diagnose path, so a shape it does not recognise is
                ignored rather than allowed to fail a supervisor's request. Losing a
                measurement is a smaller harm than losing the answer it measured.
            model: The requested model id.

        A count that is absent records nothing, rather than zero. Zero is a measurement
        and would drag an average down with calls that were never made; absence is the
        honest reading of a field the model did not populate.
        """
        if usage is None:
            return
        base = self._attributes(model)
        for field, token_type in _TOKEN_FIELDS.items():
            value = getattr(usage, field, None)
            if isinstance(value, int) and value > 0:
                self._tokens.record(value, {**base, "gen_ai.token.type": token_type})

    def record_operation(
        self,
        seconds: float,
        *,
        model: str,
        error_type: str | None = None,
    ) -> None:
        """Record how long one investigation took, and how it ended.

        Args:
            seconds: Wall clock, including the tool round trips and any retry.
            model: The requested model id.
            error_type: The exception class name when the investigation failed, or None.
                Recorded as ``error.type``, which is what makes a success rate possible;
                a dashboard that only shows successful calls is how a broken agent looks
                healthy. The attribute is **absent** on success rather than empty, because
                an empty string is a distinct series and would split every query.
        """
        attributes = self._attributes(model)
        if error_type:
            attributes["error.type"] = error_type
        self._duration.record(max(seconds, 0.0), attributes)

    @staticmethod
    def _attributes(model: str) -> dict[str, str]:
        return {
            "gen_ai.operation.name": _OPERATION,
            "gen_ai.provider.name": _PROVIDER,
            "gen_ai.request.model": model,
        }
