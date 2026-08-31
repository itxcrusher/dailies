"""Reconstruct the board's rows from telemetry instead of from process memory.

The board previously read a :class:`~dailies_api.state.ShotStore` that nothing ever
wrote to, so the hosted page said *"No shots are being watched yet"* however many
renders had run. Seeding the store would not have fixed it either: Cloud Run scales the
API to zero, so anything held in process memory is gone between two visits, and a judge
arriving cold would still find an empty page.

Deriving the rows from Prometheus **removes** that state rather than relocating it.
Grafana already holds the authoritative answer to "which shots exist and how far along
are they", it is written by the renders themselves, and it survives a cold start because
it is not our process. It also means the board cannot drift from the telemetry the
investigator reasons over: both read the same series, so the board can never show a shot
the agent cannot investigate, or a frame count the agent would contradict.

The cost is one Grafana round trip per page load, which is the right trade for a board
whose whole claim is that it reflects the farm rather than a cache of it.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from dailies_telemetry.schema import METRICS, Metric

from .duration_sample import sample_from_buckets
from .state import Shot
from .windows import LOOKBACK, STEP_SECONDS

__all__ = ["LOOKBACK", "STEP_SECONDS", "GrafanaShotSource"]

_log = logging.getLogger(__name__)


#: The identity labels a shot id is built from, in id order. The same four telemetry keys
#: every render series by, which is what makes this reconstruction exact rather than a
#: heuristic: an id built here is the id the render would have produced.
_IDENTITY = Shot.ID_FIELDS


class _Queryable(Protocol):
    """The one method this module needs from :class:`~dailies_api.mcp_client.GrafanaMCP`."""

    async def query_prometheus(self, expr: str, **kwargs: Any) -> Any: ...


def _last_value(entry: dict[str, Any]) -> float | None:
    """Read the most recent sample out of one series, range- or instant-shaped.

    The MCP server returns ``values`` (a list of pairs) for a range query and ``value``
    (a single pair) for an instant one. Accepting both keeps this working if the query
    shape below is ever changed, rather than silently returning an empty board.
    """
    points = entry.get("values")
    if points:
        pair = points[-1]
    else:
        pair = entry.get("value")
    if not pair or len(pair) < 2:
        return None
    try:
        return float(pair[1])
    except (TypeError, ValueError):
        return None


def _key(labels: dict[str, Any]) -> tuple[str, ...] | None:
    """The four identity label values, or ``None`` if any is missing or empty."""
    values = tuple(str(labels.get(name, "")) for name in _IDENTITY)
    return values if all(values) else None


class GrafanaShotSource:
    """Builds :class:`~dailies_api.state.Shot` rows from the render progress series."""

    def __init__(self, grafana: _Queryable) -> None:
        self._grafana = grafana
        #: What the delivery rating needs beyond frame counts, keyed by shot id: the due
        #: date and the observed frame costs. Populated by :meth:`list_shots` rather than
        #: returned alongside the shots, because :class:`~dailies_api.state.Shot` is the
        #: board's contract and should not grow a field for every intermediate the rating
        #: happens to want.
        self.telemetry: dict[str, dict[str, Any]] = {}

    async def _series(self, metric: str) -> dict[tuple[str, ...], float]:
        """Query one metric and index its series by identity.

        A **range** query, never an instant one, and that is not a style choice. An
        instant query returns nothing for a job that has finished, because the series
        falls outside Prometheus's staleness window. Every shot here is a batch render
        that ends, so an instant query would empty the board minutes after each render
        and look exactly like "no renders have happened".
        """
        response = await self._grafana.query_prometheus(
            metric,
            start_time=LOOKBACK,
            end_time="now",
            step_seconds=STEP_SECONDS,
            query_type="range",
        )
        entries = response.get("data") if isinstance(response, dict) else None
        indexed: dict[tuple[str, ...], float] = {}
        for entry in entries or []:
            labels = entry.get("metric") or {}
            key = _key(labels)
            if key is None:
                # One odd series must not blank the whole board. Shot.make_id refuses an
                # empty component, and raising here would turn a single malformed series
                # into an empty page, which is the failure this module exists to end.
                _log.warning("Skipping a %s series with incomplete identity: %r", metric, labels)
                continue
            value = _last_value(entry)
            if value is not None:
                indexed[key] = value
        return indexed

    async def list_shots(self) -> list[Shot]:
        """Every shot with telemetry in the lookback window, newest identity order.

        Driven by ``frames_expected`` rather than by the completion counter: a job that
        has declared its range but finished no frames yet is precisely the one a
        supervisor wants on the board, and keying off completions would hide it until
        the first frame landed.
        """
        expected = await self._series(METRICS[Metric.FRAMES_EXPECTED])
        completed = await self._series(METRICS[Metric.FRAMES_COMPLETED])
        deadlines = await self._series(METRICS[Metric.DEADLINE])
        buckets = await self._histogram(f"{METRICS[Metric.FRAME_DURATION]}_bucket")

        self.telemetry = {}
        shots: list[Shot] = []
        for key, frames_total in expected.items():
            try:
                shot_id = Shot.make_id(*key)
            except ValueError:
                _log.warning("Skipping a series whose labels do not form an id: %r", key)
                continue
            shots.append(
                Shot(
                    id=shot_id,
                    frames_total=int(frames_total),
                    frames_done=int(completed.get(key, 0)),
                )
            )
            self.telemetry[shot_id] = {
                # `.get` rather than a default: a shot with no deadline series has no due
                # date, and None must survive as None. A 0 here would read as 1970, the
                # most overdue any shot can be, and redden every undated render.
                "deadline_epoch": int(deadlines[key]) if key in deadlines else None,
                "durations": sample_from_buckets(buckets.get(key, {})),
            }
        return sorted(shots, key=lambda shot: shot.id)

    async def _histogram(self, metric: str) -> dict[tuple[str, ...], dict[float, float]]:
        """Index a histogram's bucket counts by shot identity, then by ``le``.

        Separate from :meth:`_series` because a bucket series carries one label the
        others do not, ``le``, and collapsing on identity alone would have every bucket
        of a shot overwrite the last and leave one arbitrary count standing.
        """
        response = await self._grafana.query_prometheus(
            metric,
            start_time=LOOKBACK,
            end_time="now",
            step_seconds=STEP_SECONDS,
            query_type="range",
        )
        entries = response.get("data") if isinstance(response, dict) else None
        indexed: dict[tuple[str, ...], dict[float, float]] = {}
        for entry in entries or []:
            labels = entry.get("metric") or {}
            key = _key(labels)
            if key is None:
                continue
            try:
                # "+Inf" is how Prometheus spells the overflow bucket; float() reads it.
                bound = float(labels.get("le"))
            except (TypeError, ValueError):
                continue
            value = _last_value(entry)
            if value is not None:
                indexed.setdefault(key, {})[bound] = value
        return indexed
