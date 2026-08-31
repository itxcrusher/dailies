/**
 * The states the Diagnose button moves through, and what each one says.
 *
 * Split out from the component so the wording is testable without a DOM. The wording is
 * the part that matters: an investigation takes 20 to 50 seconds against a live model
 * and Grafana, which is long enough that a button giving no feedback reads as broken,
 * and long enough that someone will press it again. Both are avoidable by saying what is
 * happening and roughly how long it takes.
 */

export type DiagnoseState = "idle" | "running" | "error" | "cached";

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
 * How long until this shot can be investigated again, in words.
 *
 * The cooldown is right to exist - the route is public, shot ids are enumerable, and
 * every press costs a model call - but a button that silently returns the previous
 * answer looks broken, and a supervisor who cannot tell "unchanged" from "nothing
 * happened" presses it again. Which is the behaviour the cooldown exists to stop.
 */
export function cooldownNote(ageSeconds: number, cooldownSeconds = 300): string {
  const remaining = Math.max(0, cooldownSeconds - ageSeconds);
  const answered = ageSeconds < 60 ? "just now" : `${Math.round(ageSeconds / 60)}m ago`;
  if (remaining <= 0) return `Answered ${answered}.`;
  const wait = remaining < 60 ? `${remaining}s` : `${Math.ceil(remaining / 60)}m`;
  return `Answered ${answered}. Ask again in ${wait}.`;
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
  if (state === "cached" && error) return error;
  return null;
}
