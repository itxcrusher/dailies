#!/bin/sh
# Assemble the OTLP credential, then hand off to the worker.
#
# This exists for one reason. Grafana Cloud wants "Authorization: Basic <base64 of
# instance_id:token>", and Secret Manager holds the raw "<instance_id>:<token>" pair.
# Base64-ing it in Terraform would mean reading the secret VALUE into Terraform, which
# writes the credential in plaintext into terraform.tfstate and into the Cloud Run
# revision spec. Doing it here keeps the credential in Secret Manager: the container gets
# the pair by reference and encodes it in its own memory.
#
# The percent-encoded space in the prefix is not a typo. The Python OTLP exporter parses
# OTEL_EXPORTER_OTLP_HEADERS as a comma-separated key=value list and cuts the value at a
# literal space, so "Basic <b64>" arrives truncated and Grafana answers 401 with nothing
# pointing at the cause.
#
# Nothing here echoes the credential, and there is deliberately no `set -x`.

set -eu

if [ -n "${GRAFANA_OTLP_AUTH:-}" ]; then
    OTEL_EXPORTER_OTLP_HEADERS="${DAILIES_OTLP_HEADER_PREFIX:-Authorization=Basic%20}$(printf %s "$GRAFANA_OTLP_AUTH" | base64 -w0)"
    export OTEL_EXPORTER_OTLP_HEADERS
else
    # Loud, and still a render. A local `docker run` has no Grafana credential and should
    # render anyway; a Cloud Run execution that lost its secret binding must not look
    # identical to a healthy one in the log.
    echo "dailies: GRAFANA_OTLP_AUTH is unset; metrics will not reach Grafana Cloud" >&2
fi

# exec so the worker is PID 1: it receives Cloud Run's SIGTERM directly, and its exit
# code is the execution's exit code. A shell in the middle would swallow both.
exec python -m dailies_render.worker "$@"
