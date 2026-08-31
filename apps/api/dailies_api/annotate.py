"""Putting a finding on the Grafana timeline, next to the metrics that produced it.

The board answers "what is wrong with this shot". The timeline answers a different
question that nobody could ask before: "what has this farm been told about itself, and
when". A supervisor scrubbing a dashboard back through the night sees the marks where the
agent found something, without having opened the board at all.

**Why this is code and not a sentence in the prompt.** The agent has always had
``create_annotation`` in its toolset, ``mcp_client`` has always implemented it, and the
instructions have always mentioned that an annotation "puts your conclusion on the
Grafana timeline". Measured on 2026-08-31: zero annotations in seven days across
seventeen investigations. The instruction describes the tool rather than asking for it, so
a model told to diagnose does exactly that and stops.

That is the same lesson as the evidence schema, which refuses an answer with no queries
behind it rather than requesting them politely. A property of the system cannot rest on
whether the model felt like calling a tool. The tool stays in the agent's kit for its own
use mid-investigation; this is the floor underneath it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = ["ANNOTATION_TAG", "annotation_for", "build_annotator", "should_annotate"]

_log = logging.getLogger(__name__)

#: Tag on every annotation this writes, so the dashboard can query for exactly these and
#: a human can tell an agent's mark from an operator's.
ANNOTATION_TAG = "dailies"

#: Longest cause text carried into the annotation. Grafana renders hover text in a small
#: tooltip, and a paragraph there is a paragraph nobody reads; the full diagnosis with its
#: evidence is on the board, which is where someone goes when the mark tells them to.
_MAX_CAUSE = 240


def should_annotate(diagnosis: dict[str, Any] | None) -> bool:
    """Whether this diagnosis is worth a mark on the timeline.

    Only a found problem. A timeline marked on every shot is a timeline nobody reads: the
    mark's whole meaning is "something is wrong here", and spending it on a clean render
    costs that meaning for the shots that need it.

    A missing ``problem_found`` is not a found problem. Older stored answers predate the
    field, and defaulting an absent verdict to true would mark the timeline on the
    strength of something nobody ever set.
    """
    return bool(diagnosis) and diagnosis.get("problem_found") is True


def annotation_for(
    shot_id: str,
    diagnosis: dict[str, Any],
    *,
    now_epoch: float | None = None,
) -> dict[str, Any]:
    """The annotation payload for one finding.

    Pure, and separated from the transport so the wording and the unit are testable
    without a Grafana. ``now_epoch`` is seconds, as :func:`time.time` returns them.
    """
    moment = time.time() if now_epoch is None else now_epoch
    shot = diagnosis.get("shot") or shot_id.split(":")[-2 if shot_id.count(":") >= 2 else -1]
    cause = str(diagnosis.get("cause") or "A problem was found.").strip()
    if len(cause) > _MAX_CAUSE:
        cause = cause[: _MAX_CAUSE - 1].rstrip() + "…"
    confidence = diagnosis.get("confidence")
    suffix = f" ({confidence} confidence)" if confidence else ""
    return {
        "text": f"Dailies: {shot} - {cause}{suffix}",
        # Milliseconds, and named for the unit. Grafana takes epoch ms; passing seconds
        # puts the mark in 1970 and the API still answers 200, so the failure is a silent
        # wrong answer rather than an error. That is this repo's recurring shape, which is
        # why a test pins it.
        "time_ms": int(moment * 1000),
        # The shot is a tag as well as text, so a dashboard can filter the timeline to one
        # shot instead of showing every mark the farm ever produced.
        "tags": [ANNOTATION_TAG, str(shot)],
    }


def build_annotator(
    connect: Callable[[str], Any],
    url: str,
    *,
    prometheus_uid: str | None = None,
    loki_uid: str | None = None,
) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
    """An annotator that writes through the Grafana MCP server.

    Through MCP rather than Grafana's HTTP API, and not for convenience: this project's
    hard constraint is that Grafana is used at runtime through the MCP server, imported
    and called in code rather than named in documentation. A direct REST write would be
    the easier path and would quietly step outside that.

    A session per call, matching the shot source. Cloud Run freezes an instance between
    requests and a socket held across that comes back dead in a way that surfaces as a
    slow failure later rather than a clean reconnect.
    """
    from .mcp_client import GrafanaMCP

    async def annotate(shot_id: str, diagnosis: dict[str, Any]) -> None:
        payload = annotation_for(shot_id, diagnosis)
        async with connect(url) as session:
            grafana = GrafanaMCP(session, prometheus_uid=prometheus_uid, loki_uid=loki_uid)
            await grafana.create_annotation(
                text=payload["text"],
                time_ms=payload["time_ms"],
                tags=payload["tags"],
            )
        _log.info("Annotated the Grafana timeline for %s", shot_id)

    return annotate
