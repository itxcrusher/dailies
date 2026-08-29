"""Ship the render events that metrics cannot express, as OTLP logs.

A counter can say a frame failed. It cannot say *"the frame rendered successfully and
the jacket is grey because a texture was missing"*. That sentence is the entry's
differentiator, and it only exists as a log line: Blender exits 0 while printing a
warning. The investigator's diagnosis schema requires an evidence citation for every
claim it makes, and for that class of defect the log line is the only evidence there is.

Two design decisions worth the reader's attention, both learned from a real run rather
than reasoned from first principles:

**Records are emitted as they are parsed, not batched.** A kernel OOM kill SIGKILLs the
process: no atexit, no ``finally``, no final flush. Whatever has already left the process
is the entire record of the failure. ``SimpleLogRecordProcessor`` ships each record at
call time, which costs a request per interesting line and buys the only thing that
matters here, that the last line before the kill survives. Batching would lose exactly
the run whose telemetry matters most.

**Only render-domain conditions become logs.** A 97-line Blender render must not become
97 log records. Progress is already a metric (``render_frame_duration_seconds``,
``render_worker_memory_bytes``); duplicating it as text costs Loki quota and buries the
one line an investigator needs. Frame starts and completions are dropped here on purpose.
"""

from __future__ import annotations

from typing import Final

from opentelemetry._logs import SeverityNumber
from opentelemetry.sdk._logs import LoggerProvider

from .schema import EventKind, RenderEvent

__all__ = ["LOGGED_KINDS", "RenderLogEmitter"]

#: The event kinds that become log records. Everything else is already a metric.
#:
#: The membership test is the whole noise/signal policy, so it lives here as data rather
#: than as an ``if`` buried in :meth:`RenderLogEmitter.record`: adding a new render-domain
#: condition to :class:`~dailies_telemetry.schema.EventKind` should be a one-line decision
#: about whether an investigator would ever cite it.
LOGGED_KINDS: Final[frozenset[EventKind]] = frozenset(
    {
        EventKind.ASSET_MISSING,
        EventKind.OOM,
        EventKind.ENGINE_CRASH,
        EventKind.FRAME_FAILED,
    }
)

#: ``ASSET_MISSING`` is a warning, not an error, and the distinction is load-bearing.
#: The render *succeeded*; the deliverable is wrong. An investigator filtering for
#: severity>=ERROR would miss the defect this project exists to catch, so the two must be
#: separable by a query rather than only by reading the message text.
_SEVERITY: Final[dict[EventKind, SeverityNumber]] = {
    EventKind.ASSET_MISSING: SeverityNumber.WARN,
    EventKind.OOM: SeverityNumber.ERROR,
    EventKind.ENGINE_CRASH: SeverityNumber.ERROR,
    EventKind.FRAME_FAILED: SeverityNumber.ERROR,
}


class RenderLogEmitter:
    """Emit render-domain conditions as OTLP log records.

    The provider is injected rather than built here, so tests drive it with an in-memory
    exporter and no network, and the deployment configures the endpoint and credential
    through the SDK's own ``OTEL_EXPORTER_OTLP_*`` variables.
    """

    def __init__(self, logger_provider: LoggerProvider) -> None:
        self._provider = logger_provider
        self._logger = logger_provider.get_logger("dailies.render")

    def record(self, event: RenderEvent) -> None:
        """Ship ``event`` if it is a condition an investigator would cite."""
        if event.kind not in LOGGED_KINDS:
            return

        # The message may be absent on a synthesised event; the kind is always meaningful,
        # so fall back to it rather than shipping an empty body an investigator cannot
        # quote in its evidence.
        body = event.message or f"render event: {event.kind.value}"

        self._logger.emit(
            self._build(
                body=body,
                severity=_SEVERITY[event.kind],
                attributes={
                    "event_kind": event.kind.value,
                    "shot": event.shot,
                    "frame": event.frame,
                    "project": event.project,
                    "sequence": event.sequence,
                    "render_job": event.render_job,
                    "worker": event.worker,
                },
            )
        )

    def flush(self) -> None:
        """Force any pending records out. A no-op under the simple processor."""
        self._provider.force_flush()

    @staticmethod
    def _build(*, body: str, severity: SeverityNumber, attributes: dict[str, object]):
        """Construct a LogRecord across SDK versions.

        The SDK moved ``LogRecord`` between ``opentelemetry.sdk._logs`` and the private
        ``_logs._internal`` module, and the constructor's accepted keywords have changed.
        Importing at call time and passing only the stable keywords keeps this working
        across the range our pin allows, rather than breaking on a patch upgrade.
        """
        from opentelemetry.sdk._logs import LogRecord

        return LogRecord(
            body=body,
            severity_number=severity,
            severity_text=severity.name,
            attributes=attributes,
        )
