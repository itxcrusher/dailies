"""How far back this system considers "recent".

One constant, imported by everything that queries. It has to be shared rather than
restated, and the reason is a bug that was already paid for: the board listed shots over
24 hours while the investigator's prompt asked for the last 30 to 60 minutes, so the
board showed a shot the agent then reported as having no telemetry. Both components were
correct on their own and the pair was incoherent.

It lives here rather than in :mod:`dailies_api.shot_source` because the MCP client
defaults its log queries to this window too, and a client that imports the shot source to
learn about time is backwards layering.
"""

from __future__ import annotations

__all__ = ["LOOKBACK", "STEP_SECONDS"]

#: How far back to look for renders, in the relative form the Grafana MCP server accepts.
#:
#: Wide enough that a shot rendered earlier in the session is still on the board, narrow
#: enough that a demo does not fill with weeks of history. A render is a batch job that
#: ends, so "recent" here means "recently ran", not "currently running".
LOOKBACK = "now-24h"

#: Range-query resolution.
#:
#: At or below Prometheus's five-minute lookback delta, and that ceiling is not a style
#: choice. A render lasts seconds, so a wider step steps straight over it: measured on
#: this stack, the same query at step 3600 and 900 returned zero series while step 300
#: returned the value.
STEP_SECONDS = 300
