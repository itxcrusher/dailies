"""Publish `dailies.json` to Grafana, and refuse to do it with panels that show nothing.

A dashboard is the one artefact where being wrong is invisible. Every panel renders
something: a query against a metric that does not exist, or one whose samples all fall
outside the staleness window, draws an empty chart that looks exactly like a quiet
pipeline. This repo has been caught by that seven times, so publishing runs every panel's
own query first and stops if any of them comes back empty.

Usage::

    export GRAFANA_URL=https://<stack>.grafana.net
    export GRAFANA_SERVICE_ACCOUNT_TOKEN=...      # never written to a file
    python dashboards/publish.py                  # --force to publish anyway

The token comes from the environment because it is the same secret the API already reads
from Secret Manager, and nothing here should be the second place it lives.

**On `last_over_time`.** Every panel wraps its metric in `last_over_time(m[$__range])`
rather than querying it bare, and that is not a style choice. Prometheus only answers an
instant query from a sample inside its five-minute staleness window, and a render that
finished an hour ago has none. Measured on this stack: `render_job_frames_expected`
returns 6 series over a 24h range and **0** as an instant query. Bare metrics would have
given a dashboard that is correct during a render and empty every other minute, which is
the worst of both, because it looks fine exactly when someone is testing it.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

DASHBOARD = pathlib.Path(__file__).with_name("dailies.json")
PROMETHEUS_UID = "grafanacloud-prom"


def _post(url: str, token: str, payload: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def points_for(base: str, token: str, expr: str, *, instant: bool) -> int:
    """How many points this panel's own query returns over the last 24 hours."""
    now = int(time.time() * 1000)
    body = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"type": "prometheus", "uid": PROMETHEUS_UID},
                "expr": expr.replace("$__range", "24h"),
                "instant": instant,
                "range": not instant,
                "intervalMs": 60_000,
                "maxDataPoints": 500,
            }
        ],
        "from": str(now - 86_400_000),
        "to": str(now),
    }
    frames = _post(f"{base}/api/ds/query", token, body)["results"]["A"].get("frames", [])
    usable = 0
    for frame in frames:
        columns = frame.get("data", {}).get("values") or []
        # The VALUE column, not the timestamp column, and NaN does not count. A panel can
        # come back with a full set of points that are every one of them NaN, which draws
        # an empty chart while satisfying any check that only counts rows. That is exactly
        # what histogram_quantile over increase() does to the render duration: a render is
        # a short-lived job with one sample per series, so increase has nothing to
        # extrapolate from. Counting rows would have passed it.
        values = columns[-1] if columns else []
        usable += sum(1 for v in values if isinstance(v, (int, float)) and not math.isnan(v))
    return usable


def main() -> int:
    force = "--force" in sys.argv
    try:
        base = os.environ["GRAFANA_URL"].rstrip("/")
        token = os.environ["GRAFANA_SERVICE_ACCOUNT_TOKEN"]
    except KeyError as missing:
        print(f"Set {missing} first; see this file's docstring.", file=sys.stderr)
        return 2

    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))

    # NaN counts as empty; see points_for.
    empty = []
    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        for target in panel.get("targets", []):
            found = points_for(base, token, target["expr"], instant=target.get("instant", False))
            status = "ok" if found else "EMPTY"
            print(f"  {status:<5} {panel['title'][:44]:<44} {target['refId']}  {found} points")
            if not found:
                empty.append(f"{panel['title']} [{target['refId']}]")

    if empty and not force:
        print("\nRefusing to publish. These panels would render as an empty chart:")
        for name in empty:
            print(f"  - {name}")
        print("\nRun a render first, or pass --force if the emptiness is expected.")
        return 1

    result = _post(
        f"{base}/api/dashboards/db",
        token,
        {"dashboard": dashboard, "overwrite": True, "message": "dailies dashboard"},
    )
    print(f"\n{result.get('status')}: {base}{result.get('url')} (version {result.get('version')})")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:  # a 401 here is almost always an expired token
        print(f"Grafana refused the request: {exc.code} {exc.read()[:200]!r}", file=sys.stderr)
        raise SystemExit(2) from exc
