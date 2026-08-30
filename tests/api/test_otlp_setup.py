"""Assembling the OTLP credential for the API process.

The render container does this in a shell entrypoint; the API has no entrypoint script,
so it does the same thing in Python at provider construction. The credential still never
leaves the process: Secret Manager holds the raw "instance_id:token" pair, Terraform
passes it by reference, and the base64 happens here in memory. Encoding it in Terraform
instead would write the credential in plaintext into terraform.tfstate and into the Cloud
Run revision spec.
"""

import base64

from dailies_api.otlp import otlp_headers


def test_the_pair_is_base64_encoded_with_the_basic_prefix():
    header = otlp_headers({"GRAFANA_OTLP_AUTH": "123456:glc_token"})
    assert header is not None
    prefix, _, encoded = header.partition("Basic%20")
    assert prefix == "Authorization="
    assert base64.b64decode(encoded).decode() == "123456:glc_token"


def test_the_space_after_basic_is_percent_encoded():
    """Not a typo, and the reason is worth keeping in a test.

    The Python OTLP exporter parses OTEL_EXPORTER_OTLP_HEADERS as a comma-separated
    key=value list and cuts the value at a literal space, so "Basic <b64>" arrives
    truncated and Grafana answers 401 with nothing pointing at the cause.
    """
    header = otlp_headers({"GRAFANA_OTLP_AUTH": "123456:glc_token"})
    assert "Basic%20" in (header or "")
    assert "Basic " not in (header or "")


def test_no_credential_yields_no_header_rather_than_an_empty_one():
    """An empty Authorization header is worse than none: it fails as a 401 rather than
    as an obviously unconfigured export."""
    assert otlp_headers({}) is None
    assert otlp_headers({"GRAFANA_OTLP_AUTH": ""}) is None
    assert otlp_headers({"GRAFANA_OTLP_AUTH": "   "}) is None


def test_an_already_configured_header_is_left_alone():
    """A deployment that sets the header itself must win over this convenience."""
    env = {"GRAFANA_OTLP_AUTH": "1:2", "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=Bearer%20abc"}
    assert otlp_headers(env) == "Authorization=Bearer%20abc"


def test_the_credential_is_never_returned_in_the_clear():
    header = otlp_headers({"GRAFANA_OTLP_AUTH": "123456:glc_supersecret"})
    assert "glc_supersecret" not in (header or "")
