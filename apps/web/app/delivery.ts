/**
 * How the board words a delivery estimate.
 *
 * Kept pure and separate from the table so the wording is testable, because the wording
 * is where this can mislead. A supervisor glancing at a row needs to know whether the
 * shot makes its date, and by how much, without doing arithmetic on two epoch seconds.
 */

export type Confidence = "high" | "medium" | "low" | "unknown";

/** A compact duration: "2h 14m", "45m", "30s". Never "0m" for a non-zero span. */
export function humanDuration(seconds: number): string {
  const s = Math.abs(Math.round(seconds));
  if (s < 60) return `${s}s`;
  const minutes = Math.floor(s / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return rest ? `${hours}h ${rest}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  const restHours = hours % 24;
  return restHours ? `${days}d ${restHours}h` : `${days}d`;
}

/**
 * The line under the risk pill.
 *
 * `null` when there is nothing honest to say. That is the common case at the top of a
 * render and it must not be filled with a placeholder: a row reading "0m spare" when
 * nobody has estimated anything is a claim, and the wrong one.
 */
export function describeSlack(slackSeconds: number | null | undefined): string | null {
  if (slackSeconds === null || slackSeconds === undefined) return null;
  if (slackSeconds < 0) return `${humanDuration(slackSeconds)} late`;
  return `${humanDuration(slackSeconds)} spare`;
}

/**
 * How to qualify an ETA, or `null` when it needs no qualifier.
 *
 * "high" is deliberately silent. Annotating the confident case trains the eye to skip
 * the annotation, which is exactly when the "low" that matters stops being read.
 */
export function describeConfidence(confidence: Confidence | string | null | undefined): string | null {
  if (confidence === "low") return "low-confidence estimate";
  if (confidence === "medium") return "rough estimate";
  return null;
}

/** Absolute clock time for an ETA, in the viewer's locale, or null if there is none. */
export function formatEta(etaEpoch: number | null | undefined): string | null {
  if (etaEpoch === null || etaEpoch === undefined) return null;
  return new Date(etaEpoch * 1000).toLocaleString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    day: "numeric",
    month: "short",
  });
}
