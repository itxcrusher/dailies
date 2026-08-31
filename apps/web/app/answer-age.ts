/**
 * How old an answer is, and whether the agent that produced it is still the one running.
 *
 * A diagnosis is a claim about a render at a moment. Shown with no date it reads as a
 * statement about now, which is how SH200 spent days on this board asserting "completed
 * successfully with no logged errors" from queries that named a metric which does not
 * exist. The agent had been fixed; the board was replaying the old conclusion and had no
 * way to say so.
 *
 * Both functions are pure and take `now` as an argument rather than reading the clock, so
 * the wording is testable and so a server render and a client render of the same shot
 * cannot disagree about what "3m ago" means.
 */

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/**
 * "just now", "45m ago", "3h ago", "3d ago", or null when nobody has answered.
 *
 * Hours run to 47 before switching to days. A render that finished yesterday evening is
 * more usefully "19h ago" than "1d ago", which rounds away the part a supervisor is
 * actually judging: whether the answer predates the shift they are looking at.
 */
export function answeredAgo(
  answeredAt: number | null | undefined,
  nowEpoch: number,
): string | null {
  if (answeredAt === null || answeredAt === undefined) return null;
  // Clamped at zero. The API stamps with its clock and the browser renders with the
  // viewer's, so a few seconds of skew is normal and "-1m ago" would read as a bug in the
  // very thing the board is asking to be trusted.
  const seconds = Math.max(0, nowEpoch - answeredAt);
  if (seconds < MINUTE) return "just now";
  if (seconds < HOUR) return `${Math.floor(seconds / MINUTE)}m ago`;
  if (seconds < 2 * DAY) return `${Math.floor(seconds / HOUR)}h ago`;
  return `${Math.floor(seconds / DAY)}d ago`;
}

/**
 * What to say about the agent behind a stored answer, or null when there is nothing to say.
 *
 * Deliberately not "outdated" or "wrong". The conclusion may still be entirely correct;
 * what is true is only that the agent producing answers today is not the one that produced
 * this, and the board is not in a position to judge which of them is right. It reports the
 * fact and leaves the Re-run button next to it.
 */
export function provenanceNote(stale: boolean): string | null {
  return stale ? "from an earlier agent" : null;
}
