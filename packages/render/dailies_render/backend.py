"""The render-farm contract Dailies is written against.

Everything above this layer (the agents, the API, the dashboards) talks to a
``RenderBackend``, never to a scheduler directly. That is what keeps the system
portable: Cloud Run is the first adapter, not the product.

The protocol is three things, not one: the method shapes, the error taxonomy every
adapter translates into, and the priority vocabulary the commands use. A seam that
declares only the first leaks the other two - callers end up catching a scheduler's
own exception types and guessing at its own priority scale, which re-couples them to
exactly the thing the protocol exists to hide.
"""

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from dailies_telemetry.schema import Priority

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "JobNotFound",
    "Priority",
    "RenderBackend",
    "TaskNotFound",
    "UnsupportedOperation",
]


class BackendError(Exception):
    """Base for every failure an adapter may raise through the protocol.

    Adapters translate; callers catch these. Without a declared taxonomy a Cloud Run
    adapter raises ``googleapiclient`` errors, an OpenCue one raises ``grpc`` errors
    and a Deadline one raises ``requests.HTTPError``, so every caller above the seam
    has to either catch bare ``Exception`` or import all three vendors' exception
    types. Both outcomes put the scheduler back into the layers this protocol keeps it
    out of, and AGENTS.md forbids that leak by name.

    Catching ``BackendError`` is therefore always sufficient; the subclasses exist so a
    caller can tell "you asked for something that is not there" from "the farm is
    down" from "this farm cannot do that at all", which are three different recoveries.
    """


class JobNotFound(BackendError):
    """No job with that id exists on the farm.

    A permanent answer about a specific id: retrying it will not help, and the caller
    should stop tracking the job rather than back off.
    """


class TaskNotFound(BackendError):
    """The job exists but has no task with that id.

    Separate from ``JobNotFound`` because the recovery differs: the job is still worth
    watching, only this task reference is stale.
    """


class BackendUnavailable(BackendError):
    """The farm could not be reached, or answered with a transport-level failure.

    The retryable one. Nothing is known about the job either way, so a caller must not
    read this as "the job is gone" - that conflation is how a transient outage turns
    into a wrongly cancelled render.
    """


class UnsupportedOperation(BackendError):
    """This scheduler does not offer the operation at all.

    Not every farm exposes per-task retry or a priority change. A partial adapter has
    three options and two of them are bad: silently no-op (the recovery agent believes
    it acted and stops escalating) or raise something undeclared (the leak above).
    Raising this is the third, and it is the only honest one.
    """


@runtime_checkable
class RenderBackend(Protocol):
    """A render farm Dailies can observe and act on.

    Deliberately scheduler-independent: Cloud Run is the first adapter, not the
    product. Flamenco, OpenCue and Deadline Cloud adapters are expected later.

    ``runtime_checkable`` so an adapter can be checked at its registration point
    rather than failing at the first call to whichever method it forgot. Note the
    limit of that check: ``isinstance`` verifies member *presence* only, never
    signatures, so it catches a missing method and not a wrong one. Static checking
    is still the real gate.

    Jobs and tasks are plain dicts on purpose. Every scheduler models them
    differently and the shape is not yet known well enough to freeze; a premature
    model here would be a lie that every adapter has to work around. Narrow it once
    the second adapter exists and the common fields are evidence rather than guess.

    Every method may raise ``BackendUnavailable``; that one is not repeated per method.
    The mutators return ``None`` and report success by *not* raising, so a caller that
    needs to know an action landed must let the exception propagate rather than
    checking a return value.
    """

    def list_jobs(self) -> list[dict]:
        """Every job the farm currently knows about, newest state included."""
        ...

    def get_job(self, job_id: str) -> dict:
        """One job's current state.

        Raises ``JobNotFound`` if no such job exists. Never returns an empty dict for a
        missing job: an absent job and a job with no fields are different facts.
        """
        ...

    def get_tasks(self, job_id: str) -> list[dict]:
        """The job's tasks, one per unit of work the scheduler split it into.

        Raises ``JobNotFound``. An existing job with no tasks yet returns ``[]``.
        """
        ...

    def retry_task(self, job_id: str, task_id: str) -> None:
        """Requeue one task.

        Raises ``JobNotFound`` / ``TaskNotFound`` for unknown ids, and
        ``UnsupportedOperation`` on a scheduler with no per-task retry. Retrying a task
        that already succeeded is the adapter's call to accept or reject, but it must
        not be silently ignored: say which in the adapter's own docstring.
        """
        ...

    def cancel_task(self, job_id: str, task_id: str) -> None:
        """Stop one task.

        Raises ``JobNotFound`` / ``TaskNotFound``, and ``UnsupportedOperation`` where
        the scheduler can only cancel whole jobs. Cancelling an already-finished task
        is a no-op, not an error: the caller's goal (it is not running) already holds.
        """
        ...

    def change_priority(self, job_id: str, priority: Priority) -> None:
        """Move the job to a different priority tier.

        ``Priority`` is a named-tier vocabulary shared with ``RenderEvent.priority``,
        so the label a dashboard rule matches on and the command the recovery agent
        sends mean the same thing. Adapters map the tier onto whatever their scheduler
        uses (Deadline's ascending integers, OpenCue's own scale); callers never see
        that number.

        Raises ``JobNotFound``, and ``UnsupportedOperation`` where priority is fixed at
        submission.
        """
        ...

    def get_output_frames(self, job_id: str) -> list[str]:
        """The frames this job has rendered so far, as absolute URIs, ordered by frame.

        URIs, not paths: the caller (the delivery board, the validation agent) must be
        able to resolve one without knowing which farm produced it, so a Cloud Run
        adapter returns ``gs://...`` and a local one returns ``file:///...``. A
        farm-local absolute path is not a valid return value - it is meaningless off
        the worker that wrote it.

        Partial output is included. A running job's finished frames are exactly what a
        delivery board needs, so this is not "the final result" but "what exists now".

        Raises ``JobNotFound``. Named ``get_output_frames`` rather than ``get_output``
        because ``get_output`` and ``get_logs`` read as synonyms while one returns
        rendered images and the other stdout.
        """
        ...

    def get_logs(self, job_id: str) -> Iterable[str]:
        """Stream the job's stdout, one line per item, in the order it was printed.

        ``Iterable``, not ``list``: a running job's log has no end yet, and the
        point of watching one is reacting before it does.

        Raises ``JobNotFound``. This is the stream ``render_from_stream`` consumes.
        """
        ...
