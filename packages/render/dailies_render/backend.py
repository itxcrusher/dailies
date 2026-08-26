"""The render-farm contract Dailies is written against.

Everything above this line (the agents, the API, the dashboards) talks to a
``RenderBackend``, never to a scheduler directly. That is what keeps the system
portable: Cloud Run is the first adapter, not the product.
"""

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

__all__ = ["RenderBackend"]


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
    """

    def list_jobs(self) -> list[dict]: ...

    def get_job(self, job_id: str) -> dict: ...

    def get_tasks(self, job_id: str) -> list[dict]: ...

    def retry_task(self, job_id: str, task_id: str) -> None: ...

    def cancel_task(self, job_id: str, task_id: str) -> None: ...

    def change_priority(self, job_id: str, priority: str) -> None: ...

    def get_output(self, job_id: str) -> list[str]: ...

    def get_logs(self, job_id: str) -> Iterable[str]:
        """Stream the job's stdout. Iterable, not ``list``: a running job's log has
        no end yet, and the point of watching one is reacting before it does."""
        ...
