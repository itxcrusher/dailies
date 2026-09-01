/**
 * What an empty board is allowed to claim.
 *
 * The board rebuilds from telemetry on every load and holds no state of its own, so a
 * Grafana failure empties it. It used to answer that with "No shots are being watched yet.
 * Run a render and this board fills itself", which is true when the farm is idle and false
 * when the telemetry source is down. Observed on 2026-09-01: a transient 503 emptied the
 * live board while three renders sat in Prometheus, and the page told the reader the farm
 * had nothing to show.
 *
 * That is this project's own thesis pointed at itself. A render that fails silently and a
 * board that fails silently are the same defect, and this one is aimed at the person who
 * would otherwise go and look for the problem.
 */

/** The line to show when there are no rows. */
export function emptyMessage(telemetryReadable: boolean): string {
  if (telemetryReadable) {
    return (
      "No shots are being watched yet. Run a render and this board fills itself from the " +
      "telemetry, with no seeding step."
    );
  }
  // Deliberately does not guess which. A datasource hiccup, an expired token and a farm
  // that genuinely rendered nothing all look identical from here, and the honest answer
  // is that the board cannot tell rather than a confident pick between them.
  return (
    "The telemetry source could not be read, so this board cannot say whether anything " +
    "is rendering. This is a failure to look, not a farm with nothing to show."
  );
}

/**
 * The line to show above rows that may no longer be current, or null.
 *
 * Only when there ARE rows: an empty board already says its piece in `emptyMessage`, and
 * stacking a second warning on the first reads as two faults instead of one.
 */
export function stalenessNotice(hasRows: boolean, telemetryReadable: boolean): string | null {
  if (!hasRows || telemetryReadable) return null;
  return "Telemetry could not be refreshed. These rows are the last known state and may be stale.";
}
