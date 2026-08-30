"""The API's own OTLP export, so the investigator is measured like everything else.

The render container assembles its Grafana credential in a shell entrypoint. The API has
no entrypoint script, so it does the same thing here, in Python, at provider construction.

The credential still never leaves the process. Secret Manager holds the raw
``instance_id:token`` pair, Terraform passes it by reference, and the base64 happens in
memory. Encoding it in Terraform instead would write the credential in plaintext into
``terraform.tfstate`` and into the Cloud Run revision spec, which is a much worse place
for it than a process's environment.
"""

from __future__ import annotations

import base64
import logging
import os
from collections.abc import Mapping

__all__ = ["build_meter_provider", "otlp_headers"]

_log = logging.getLogger(__name__)

#: The scope name for the API's own metrics, distinct from the render worker's.
SERVICE_NAME = "dailies-api"

#: How often the API pushes what it has recorded.
#:
#: Longer than the render worker's 15s: this process is long-lived behind a warm Cloud Run
#: instance, so nothing is lost to a short life, and an investigation lasts tens of
#: seconds rather than milliseconds. A 30s window still puts a diagnosis on a dashboard
#: while the person who pressed the button is still looking at it.
EXPORT_INTERVAL_MILLIS = 30_000


def otlp_headers(env: Mapping[str, str] | None = None) -> str | None:
    """The ``OTEL_EXPORTER_OTLP_HEADERS`` value for this deployment, or None.

    Returns ``None`` rather than an empty header when there is no credential. An empty
    ``Authorization`` is worse than an absent one: it fails as a 401 that looks like a
    permissions problem, where absence looks like what it is, an unconfigured export.

    The percent-encoded space in ``Basic%20`` is not a typo and is pinned by a test. The
    Python OTLP exporter parses this variable as a comma-separated ``key=value`` list and
    cuts the value at a literal space, so ``Basic <b64>`` arrives truncated and Grafana
    answers 401 with nothing pointing at the cause.
    """
    values = os.environ if env is None else env

    # A deployment that sets the header itself wins. This function is a convenience for
    # the common case, not an authority on how the process authenticates.
    existing = (values.get("OTEL_EXPORTER_OTLP_HEADERS") or "").strip()
    if existing:
        return existing

    pair = (values.get("GRAFANA_OTLP_AUTH") or "").strip()
    if not pair:
        return None

    encoded = base64.b64encode(pair.encode()).decode()
    return f"Authorization=Basic%20{encoded}"


def build_meter_provider(env: Mapping[str, str] | None = None):
    """A provider exporting the API's own metrics, or ``None`` when unconfigured.

    ``None`` rather than a no-op provider, so a caller can tell the difference and say so
    in a log. A silently-discarding provider is how an observability feature comes to be
    believed in without ever having worked.

    Imported inside the function because the OTLP exporter belongs to the ``agent`` extra;
    the read-only board routes must stay importable on the base install.
    """
    values = os.environ if env is None else env
    endpoint = (values.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    if not endpoint:
        _log.info("No OTEL_EXPORTER_OTLP_ENDPOINT; the agent's own metrics stay local")
        return None

    header = otlp_headers(values)
    if header and not (values.get("OTEL_EXPORTER_OTLP_HEADERS") or "").strip():
        # Set on the real environment, because the exporter reads it from there rather
        # than taking it as an argument. Done once, at construction, not per export.
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = header

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        _log.warning("OTLP exporter not installed; the agent's own metrics stay local")
        return None

    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(), export_interval_millis=EXPORT_INTERVAL_MILLIS
    )
    return MeterProvider(
        metric_readers=[reader],
        resource=Resource.create({"service.name": SERVICE_NAME}),
    )
