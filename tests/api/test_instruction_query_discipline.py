"""The investigator must read a batch job's metrics the way a batch job records them.

Driven on 2026-08-29, diagnosing SH050. The agent reached the right cause (a missing
jacket_diffuse.exr, cited from Loki) through two wrong metric readings:

- `sum by (shot) (render_job_frames_expected{...})` as an INSTANT query returned nothing,
  and it concluded "the job did not properly register its frame count". The real value
  is 3. That is the staleness trap: a finished job's series is not present at `now`.
- `increase(render_job_frames_completed_total{...}[24h])` returned 1.5, which it read as
  "minimal or incomplete processing". The counter went 0 -> 3 in one burst; `increase`
  extrapolates over a window and is meaningless for a batch job that ran for seconds.

Together those turned a render that COMPLETED ALL THREE FRAMES into "failing to render".
That is precisely the distinction this project exists to make: SH050 exited 0 and its
deliverable is wrong. Blurring it turns the entry's central claim into an ordinary
render-failure report.
"""

from dailies_api.agent import INVESTIGATOR_INSTRUCTION

TEXT = INVESTIGATOR_INSTRUCTION.lower()


def test_it_is_told_every_prometheus_query_must_be_a_range_query():
    assert "range" in TEXT
    assert "instant" in TEXT


def test_it_is_warned_off_rate_and_increase_on_a_batch_counter():
    assert "increase(" in TEXT or "increase" in TEXT
    assert "rate(" in TEXT or "rate" in TEXT


def test_it_is_told_completed_equals_expected_means_the_render_succeeded():
    """The signature case: success plus an asset warning is a wrong deliverable."""
    assert "exited 0" in TEXT or "completed all" in TEXT


def test_it_still_carries_the_evidence_rule():
    assert "never state a cause you have not supported" in TEXT
