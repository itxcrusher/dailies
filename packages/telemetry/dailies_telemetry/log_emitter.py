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

import logging
from typing import Final

from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler

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
_LEVEL: Final[dict[EventKind, int]] = {
    EventKind.ASSET_MISSING: logging.WARNING,
    EventKind.OOM: logging.ERROR,
    EventKind.ENGINE_CRASH: logging.ERROR,
    EventKind.FRAME_FAILED: logging.ERROR,
}


class RenderLogEmitter:
    """Emit render-domain conditions as OTLP log records.

    Records go out through :class:`~opentelemetry.sdk._logs.LoggingHandler` attached to a
    private stdlib logger, rather than by constructing ``LogRecord`` directly.

    That is not stylistic. An earlier version built ``LogRecord`` itself and died in the
    container with ``ImportError: cannot import name 'LogRecord' from
    'opentelemetry.sdk._logs'``, while passing locally, because the dependency was pinned
    ``>=1.30`` and the container resolved a newer SDK that had moved the class. The whole
    render then exited 1. ``LoggingHandler`` is the documented integration point and has
    not moved, so the version drift that broke the render cannot recur here.

    The logger is private (``propagate = False``, a unique name) so these records never
    reach the root logger and appear twice in Cloud Logging alongside Blender's own
    stdout, which the container already captures.
    """

    def __init__(self, logger_provider: LoggerProvider) -> None:
        self._provider = logger_provider
        # A per-instance logger name, not a shared one. logging.getLogger is a singleton
        # registry: two emitters over two providers asking for the same name get the same
        # logger, and whichever attached its handler first captures both their records.
        # That is not hypothetical - it silently sent one provider's records to another's
        # exporter until a test caught it.
        self._log = logging.getLogger(f"dailies.render.events.{id(self):x}")
        self._log.setLevel(logging.WARNING)
        self._log.propagate = False
        self._log.handlers.clear()
        self._log.addHandler(LoggingHandler(level=logging.WARNING, logger_provider=logger_provider))

    def record(self, event: RenderEvent) -> None:
        """Ship ``event`` if it is a condition an investigator would cite."""
        if event.kind not in LOGGED_KINDS:
            return

        # The message may be absent on a synthesised event; the kind is always meaningful,
        # so fall back to it rather than shipping an empty body an investigator cannot
        # quote in its evidence.
        body = event.message or f"render event: {event.kind.value}"

        self._log.log(
            _LEVEL[event.kind],
            body,
            extra={
                "event_kind": event.kind.value,
                "shot": event.shot,
                "frame": event.frame,
                "project": event.project,
                "sequence": event.sequence,
                "render_job": event.render_job,
                "worker": event.worker,
            },
        )

    def flush(self) -> None:
        """Force any pending records out. A no-op under the simple processor."""
        self._provider.force_flush()
