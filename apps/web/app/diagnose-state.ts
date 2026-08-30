/**
 * The states the Diagnose button moves through, and what each one says.
 *
 * Split out from the component so the wording is testable without a DOM. The wording is
 * the part that matters: an investigation takes 20 to 50 seconds against a live model
 * and Grafana, which is long enough that a button giving no feedback reads as broken,
 * and long enough that someone will press it again. Both are avoidable by saying what is
 * happening and roughly how long it takes.
 */

export type DiagnoseState = "idle" | "running" | "error";

/** The shot's diagnose endpoint. Kept pure so the URL shape is pinned by a test. */
export function diagnoseUrl(apiBase: string, shotId: string): string {
  // The id carries colons (project:sequence:shot:render_job). They are legal in a path
  // segment, but encoding is still right: it is what keeps a future id containing a
  // slash or a space from silently addressing a different route.
  return `${apiBase.replace(/\/+$/, "")}/api/shots/${encodeURIComponent(shotId)}/diagnose`;
}

/** What the button reads in each state. */
export function buttonLabel(state: DiagnoseState, hasDiagnosis: boolean): string {
  if (state === "running") return "Investigating...";
  if (state === "error") return "Retry";
  return hasDiagnosis ? "Re-run" : "Diagnose";
}

/**
 * The line under the button, or null when there is nothing worth saying.
 *
 * Only shown while running. An idle button explains itself; a running one has to set an
 * expectation, or a thirty-second wait looks like a hang.
 */
export function statusLine(state: DiagnoseState, error: string | null): string | null {
  if (state === "running") return "Querying Prometheus and Loki through Grafana MCP, about 30s";
  if (state === "error" && error) return error;
  return null;
}
