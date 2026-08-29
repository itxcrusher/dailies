"""The production graph: which shots wait on which, and how much room each one has.

A render farm's own monitoring can tell you that a shot is running twenty minutes slow. It
cannot tell you whether that matters, because it does not know that eleven other shots are
waiting on the output. This module is the piece that knows. It is what separates
"infrastructure is degraded" from "the morning review will be short a shot".

**Slack is the number the rest of the system is built on.** For one shot it is::

    slack = (deadline - now) - the longest dependency chain that runs through this shot

Both halves of that chain count, and getting only one half right is the mistake worth
guarding against:

- The work **after** the shot counts, because a shot that eleven others wait on cannot
  spend the whole window. Its dependents need their share of it.
- The work **before** the shot counts too, because a shot cannot start at ``now`` if its
  own upstream has not finished. Its window opens late.

That quantity, ``window - longest path through the node``, is total float in the classic
critical-path sense. Naming it that way is deliberate: this is a scheduling problem that
was solved in 1957, and a render pipeline is not special enough to need a new answer. A
shot on the critical path has, by definition, the least slack in the graph.

**Design commitments, and why:**

- **Pure.** No clock, no I/O, no network. ``now_epoch`` is always a parameter. The board
  recomputes at any timestamp it likes, a test written today still passes next year, and
  every number here is reproducible from its inputs alone. That is the property that lets
  a risk verdict be trusted rather than merely displayed.
- **Cycles are rejected at insertion**, with the offending chain named in the error. A
  cycle in a dependency graph is bad data, and the worst possible moment to discover it is
  halfway through a traversal on a live board.
- **Slack is never clamped at zero.** A negative slack is the most useful number this
  module produces: it is how far past the deadline a shot already is. Clamping would
  flatten "two minutes over" and "four hours over" into the same word.
- **Resource contention is not modelled.** Two shots with no edge between them are
  assumed able to run at the same time, which is the standard critical-path assumption.
  Farm capacity is the forecaster's job (Plan 02 Task 14 divides remaining work by worker
  count); mixing it into the ordering graph would make slack depend on how busy the farm
  happened to be when the question was asked.
- **Nothing is cached.** Every query rebuilds the adjacency and the topological order. A
  production is tens of shots, not millions, and a memo hanging off a mutable model is a
  staleness bug waiting for the first ``add_dependency`` after the first read.

**On the name** ``ShotNode``. ``dailies_api.state.Shot`` already exists and is a different
thing: the API model, carrying frame counts, a risk verdict and a diagnosis. This is a
graph node, carrying an id and a duration. They were nearly given the same name, which
would have meant any module importing both had to alias one at every call site, and would
have made a shadowing bug easy to write and hard to see. One name per concept instead.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from pydantic import BaseModel, Field, model_validator

__all__ = ["Dependency", "Production", "ShotNode", "slack_seconds"]


class ShotNode(BaseModel):
    """One shot as the graph sees it: an id and how much work is left on it.

    Deliberately thinner than :class:`dailies_api.state.Shot`. The graph needs a duration
    and an identity and nothing else; frame counts, risk and diagnosis belong to the layer
    that renders the board. Keeping them out means the graph can be reasoned about (and
    tested) without constructing a plausible render.

    The id is only required to be non-empty here. :class:`dailies_api.state.Shot` owns the
    spelling rules for a render identity, and restating its pattern in a second module
    would give the project two definitions of a legal id to keep in step.
    """

    id: str = Field(min_length=1, description="Identifies the shot within this production")
    estimated_seconds: int = Field(
        ge=0,
        description=(
            "Work still to do on this shot, in seconds, as of the moment slack is asked "
            "for. Remaining work rather than total: a shot that is 90% rendered blocks "
            "its dependents for what is left, not for what it originally cost. Plan 02 "
            "Task 14 supplies this from observed frame durations; a static estimate is "
            "the fallback before any frame has landed. Whole seconds, because slack is "
            "reported in whole seconds and sub-second precision is noise at render scale."
        ),
    )


class Dependency(BaseModel):
    """``downstream`` cannot start until ``upstream`` has finished.

    An edge, spelled as a model rather than a tuple so it survives a round trip through
    JSON with its direction still legible. ``(a, b)`` in a config file is a coin flip;
    ``upstream``/``downstream`` is not.
    """

    upstream: str = Field(min_length=1, description="The shot that must finish first")
    downstream: str = Field(min_length=1, description="The shot that waits on it")


class Production(BaseModel):
    """A set of shots, the edges between them, and the deadline they are all racing.

    Holds no clock. The deadline is an absolute epoch second and ``now`` arrives as an
    argument, so the same production answers "where do we stand" for any instant.

    The invariant has three parts: the graph is acyclic, every edge names a shot the
    production holds, and every shot is stored under a key equal to its own ``id``.
    :meth:`add_shot` and :meth:`add_dependency` enforce it on the insertion path and a
    model validator enforces it on the deserialisation path, because a production
    reconstructed from JSON has not been through either and would otherwise carry a cycle,
    a dangling edge, or a node whose ``id`` nothing reads into the first traversal.
    """

    deadline_epoch: int = Field(
        description="When the cut has to be delivered, as an absolute epoch second"
    )
    shots: dict[str, ShotNode] = Field(
        default_factory=dict, description="Every shot in the production, keyed by id"
    )
    dependencies: list[Dependency] = Field(
        default_factory=list, description="The edges, in the order they were added"
    )

    @model_validator(mode="after")
    def _shots_are_keyed_by_id_and_edges_are_resolvable_and_acyclic(self) -> Production:
        """Hold all three graph invariants on the deserialisation path, not just two.

        The dict key is the identity every traversal uses, so a node stored under a key
        that is not its own ``id`` is not a cosmetic mismatch: ``critical_path`` would
        report the key while ``slack_seconds`` only answers to the field, and the two
        halves of a board would disagree about what the shot is called.
        """
        mismatched = sorted(key for key, node in self.shots.items() if key != node.id)
        if mismatched:
            raise ValueError(
                f"shots are stored under keys that do not match their id: {', '.join(mismatched)}"
            )
        self._require_edges_resolvable()
        self._topological_order()
        return self

    def add_shot(self, shot: ShotNode) -> ShotNode:
        """Add ``shot``, or replace the one already stored under its id.

        Upsert rather than raise on a repeat, matching ``ShotStore.upsert``: a shot's
        remaining work is re-estimated continuously as frames land, so "I have a newer
        number for this shot" is the ordinary case here, not an error.
        """
        self.shots[shot.id] = shot
        return shot

    def add_dependency(self, dependency: Dependency) -> Dependency:
        """Record that ``downstream`` waits on ``upstream``.

        Adding the same edge twice stores it twice. That changes no answer (the longest
        chain takes a maximum over successors, and the topological sort counts a repeat
        into the waiting count and back out of it), and it is left alone rather than
        silently de-duplicated: quietly dropping a caller's write is the worse of the two
        surprises. It does mean a caller that re-syncs the graph on every poll must build
        a fresh :class:`Production` rather than re-adding its edges into the existing one,
        or ``dependencies`` grows without bound and drags every traversal with it.

        Raises:
            ValueError: if either end is not a shot this production holds, or if the edge
                would close a cycle. The message names the chain, since the fix is in the
                data and whoever fixes it needs to see which edge to cut.
        """
        unknown = [
            shot_id
            for shot_id in (dependency.upstream, dependency.downstream)
            if shot_id not in self.shots
        ]
        if unknown:
            raise ValueError(
                f"dependency {dependency.upstream} -> {dependency.downstream} names shots this "
                f"production does not hold: {', '.join(unknown)}"
            )
        closing = self._path_between(dependency.downstream, dependency.upstream)
        if closing is not None:
            chain = " -> ".join([*closing, dependency.downstream])
            raise ValueError(
                f"dependency {dependency.upstream} -> {dependency.downstream} would close a "
                f"cycle: {chain}"
            )
        self.dependencies.append(dependency)
        return dependency

    def longest_path_through(self, shot_id: str) -> int:
        """Total seconds of the longest dependency chain that contains ``shot_id``.

        Its own remaining work, plus the longest chain that must finish before it can
        start, plus the longest chain that cannot start until it finishes. This is the
        span the shot is pinned inside, and the deadline window minus this span is its
        slack.

        Raises:
            ValueError: if the production does not hold ``shot_id``.
        """
        if shot_id not in self.shots:
            raise ValueError(f"production does not hold shot {shot_id!r}")
        order = self._topological_order()
        before = self._longest_chain(order, self._upstream_map())
        after = self._longest_chain(reversed(order), self._downstream_map())
        return before[shot_id] + self.shots[shot_id].estimated_seconds + after[shot_id]

    def critical_path(self) -> list[str]:
        """The longest-duration chain in the graph, upstream first.

        Longest by **duration**, not by hop count: one 500-second shot outranks three
        10-second ones. This is the chain with the least slack in the production, so it is
        where a delay of any size is a delay to the delivery, and it is what a supervisor
        should be looking at while nothing is on fire yet.

        Ties are broken by shot id, so the board does not reshuffle between two equally
        long chains on consecutive polls. Empty production, empty path.
        """
        if not self.shots:
            return []
        upstream = self._upstream_map()
        downstream = self._downstream_map()
        after = self._longest_chain(reversed(self._topological_order()), downstream)

        def chain_from(shot_id: str) -> tuple[int, str]:
            """Sort key: longest remaining chain first, then id, so ties are stable."""
            return (-(self.shots[shot_id].estimated_seconds + after[shot_id]), shot_id)

        # The start is taken from the chain heads only. Ranging over every shot instead
        # would be correct only if no shot could have zero duration, and zero is the
        # ordinary end state here: ``estimated_seconds`` is work *remaining*, so it drains
        # to zero as a shot finishes. A zero-duration head ties exactly with its successor
        # on chain length, and the id tie-break would then decide whether the head stayed
        # in the path, making the same graph answer differently under two spellings.
        # A non-empty acyclic graph always has at least one head.
        heads = [shot_id for shot_id in self.shots if not upstream[shot_id]]
        path = [min(heads, key=chain_from)]
        while downstream[path[-1]]:
            path.append(min(downstream[path[-1]], key=chain_from))
        return path

    def _require_edges_resolvable(self) -> None:
        """Refuse a graph whose edges name shots ``shots`` does not hold.

        Both construction doors already refuse a dangling edge, so reaching this needs
        ``shots`` or ``dependencies`` to have been mutated in place: they are public
        fields, and ``p.shots.pop(...)`` while an edge still names the shot is the easy
        way to do it. It reports as a ``ValueError`` naming the offending ids because that
        is how every other bad-graph condition in this module reports, and a caller
        wrapping a traversal in ``except ValueError`` should not have a bare ``KeyError``
        from a private helper come through instead.
        """
        unknown = sorted(
            {
                shot_id
                for dep in self.dependencies
                for shot_id in (dep.upstream, dep.downstream)
                if shot_id not in self.shots
            }
        )
        if unknown:
            raise ValueError(
                f"dependencies name shots this production does not hold: {', '.join(unknown)}"
            )

    def _downstream_map(self) -> dict[str, list[str]]:
        """For each shot, the shots that wait on it."""
        self._require_edges_resolvable()
        adjacency: dict[str, list[str]] = {shot_id: [] for shot_id in self.shots}
        for dep in self.dependencies:
            adjacency[dep.upstream].append(dep.downstream)
        return adjacency

    def _upstream_map(self) -> dict[str, list[str]]:
        """For each shot, the shots it waits on."""
        self._require_edges_resolvable()
        adjacency: dict[str, list[str]] = {shot_id: [] for shot_id in self.shots}
        for dep in self.dependencies:
            adjacency[dep.downstream].append(dep.upstream)
        return adjacency

    def _path_between(self, start: str, target: str) -> list[str] | None:
        """A downstream route from ``start`` to ``target``, or ``None`` if there is none.

        Breadth-first, so the route reported in a cycle error is the shortest one: whoever
        reads that error wants the tightest loop they can cut, not the scenic version.
        ``start == target`` returns the single-element path, which is what makes a
        self-dependency read as the cycle it is.
        """
        downstream = self._downstream_map()
        queue: deque[list[str]] = deque([[start]])
        seen = {start}
        while queue:
            path = queue.popleft()
            if path[-1] == target:
                return path
            for next_id in downstream[path[-1]]:
                if next_id not in seen:
                    seen.add(next_id)
                    queue.append([*path, next_id])
        return None

    def _topological_order(self) -> list[str]:
        """Every shot, upstream before downstream.

        Kahn's algorithm, iteratively: :meth:`_longest_chain` is longest-path dynamic
        programming over this order, and doing it by recursion instead would put a
        production's dependency depth on Python's call stack for no gain.

        Ready shots are taken in id order, which makes the order deterministic and so
        makes every number derived from it deterministic too.

        Raises:
            ValueError: if the graph has a cycle. Both construction doors already refuse
                one, so reaching this needs ``shots`` or ``dependencies`` to have been
                mutated in place. It stays because the alternative is a silent partial
                order, and a partial order does not fail here: it produces quietly wrong
                slack for every shot the traversal never reached.
        """
        downstream = self._downstream_map()
        waiting_on = {shot_id: 0 for shot_id in self.shots}
        for dep in self.dependencies:
            waiting_on[dep.downstream] += 1
        ready = deque(sorted(shot_id for shot_id, count in waiting_on.items() if count == 0))
        order: list[str] = []
        while ready:
            shot_id = ready.popleft()
            order.append(shot_id)
            for next_id in downstream[shot_id]:
                waiting_on[next_id] -= 1
                if waiting_on[next_id] == 0:
                    ready.append(next_id)
        if len(order) != len(self.shots):
            stuck = sorted(set(self.shots) - set(order))
            raise ValueError(f"production graph contains a cycle among: {', '.join(stuck)}")
        return order

    def _longest_chain(
        self, order: Iterable[str], adjacency: dict[str, list[str]]
    ) -> dict[str, int]:
        """Seconds of the longest chain reachable from each shot, excluding the shot.

        One traversal serves both directions: pass the downstream map in reverse
        topological order to get the work that must follow a shot, and the upstream map in
        topological order to get the work that must precede it. The requirement either way
        is that ``order`` visits a shot only after every shot ``adjacency`` sends it to.

        ``max``, not ``sum``: two shots that both wait on the same upstream have no edge
        between them, so nothing says one has to wait for the other.
        """
        longest: dict[str, int] = {}
        for shot_id in order:
            longest[shot_id] = max(
                (self.shots[n].estimated_seconds + longest[n] for n in adjacency[shot_id]),
                default=0,
            )
        return longest


def slack_seconds(production: Production, shot_id: str, now_epoch: int) -> int:
    """How many seconds of room this shot has against the production deadline.

    The window from ``now_epoch`` to the deadline, minus the longest dependency chain
    running through the shot. A shot with no edges gets the whole window less its own
    work; a shot in the middle of a chain gets what the chain leaves it.

    **Negative is a real answer**, not an error and not something to clamp: it says the
    chain through this shot no longer fits before the deadline, and the size of the number
    is how badly. The Guardian reads exactly that to tell ``MISSED`` from ``CRITICAL``.

    A free function rather than a method because this is the one place a time enters the
    module, and keeping it at the door makes the purity of everything behind it visible.

    Raises:
        ValueError: if the production does not hold ``shot_id``. Silence would be worse: a
            mistyped id would otherwise report the full window as slack, which reads as
            "this shot is fine" about a shot nothing is tracking.
    """
    return (production.deadline_epoch - now_epoch) - production.longest_path_through(shot_id)
