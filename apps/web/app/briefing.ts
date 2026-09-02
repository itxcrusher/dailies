/**
 * The producer's paragraph.
 *
 * The board is an instrument: a supervisor reads it by scanning a grid. A producer asks a
 * different question, in a corridor, in about four seconds: *is anything wrong, and are we
 * going to make it?* The SPEC calls this a VIEW over agent output rather than another
 * reasoning loop, and that word decides the implementation. There is no model call here.
 *
 * That is not a shortcut, it is the correct design. Everything in this sentence has
 * already been established by something accountable: the diagnosis came from an agent that
 * had to cite its queries, the risk came from arithmetic against a deadline. Asking a model
 * to paraphrase findings it did not make would add a second place for the story to go wrong
 * and nothing a reader could check.
 *
 * The rules it follows are the same ones the rest of the project is held to:
 *
 * - **The exception leads.** A producer is scanning for the thing that needs them.
 * - **Silence is not health.** A shot nobody has diagnosed is reported as unexamined, never
 *   as clean. Absence of a diagnosis is not evidence of a good frame.
 * - **It summarises rather than enumerates.** A briefing nobody finishes reading is a grid
 *   with extra steps.
 */

// Type-only, so it is erased before node ever resolves it. A runtime import of ./shots
// would need a .ts extension for node --test and the app forbids those in source, per the
// note in tsconfig.json. The shot label is three lines; a cross-module dependency to save
// them would cost more than it saves.
import type { Shot } from "./shots";

/** How many broken shots to name before falling back to a count. */
const NAMED_LIMIT = 2;

/** `project:sequence:shot:render_job` reduced to the shot, or the raw id if it is not one. */
function label(shot: Shot): string {
  const parts = shot.id.split(":");
  return parts.length >= 3 ? parts[2] : shot.id;
}

function firstSentence(cause: string): string {
  const trimmed = cause.trim();
  const stop = trimmed.search(/[.;]\s/);
  const sentence = stop === -1 ? trimmed : trimmed.slice(0, stop);
  return sentence.length > 130 ? `${sentence.slice(0, 129).trimEnd()}…` : sentence;
}

export function briefing(shots: Shot[]): string {
  if (shots.length === 0) return "Nothing is being watched. No render has reported in the last day.";

  const broken = shots.filter((s) => s.diagnosis?.problem_found === true);
  const examined = shots.filter((s) => s.diagnosis !== null && s.diagnosis !== undefined);
  const unexamined = shots.length - examined.length;
  const late = shots.filter((s) => s.risk === "LATE");
  const missing = shots.filter((s) => s.risk === "MISSED" || s.risk === "CRITICAL");

  const parts: string[] = [];

  // The exception first, named while there are few enough to name.
  if (broken.length > 0) {
    const named = broken.slice(0, NAMED_LIMIT).map((s) => {
      const cause = s.diagnosis?.cause;
      return typeof cause === "string" && cause.trim()
        ? `${label(s)} (${firstSentence(cause)})`
        : label(s);
    });
    const rest = broken.length - named.length;
    const tail = rest > 0 ? `, and ${rest} more` : "";
    parts.push(
      `${broken.length === 1 ? "One shot has" : `${broken.length} shots have`} a defective ` +
        `deliverable: ${named.join("; ")}${tail}.`,
    );
  }

  // Deadlines. Past tense for what happened, present for what is still coming, because the
  // same number means two different things and only the wording separates them.
  if (missing.length > 0) {
    const worst = missing[0];
    parts.push(
      `${label(worst)} will miss its deadline at ${worst.frames_done} of ${worst.frames_total} frames.`,
    );
  }
  if (late.length > 0) {
    parts.push(
      `${late.length === 1 ? `${label(late[0])} landed late` : `${late.length} shots landed late`}.`,
    );
  }

  // The reassuring half, and only about shots someone actually looked at.
  const clean = examined.length - broken.length;
  if (clean > 0) {
    parts.push(`${clean} of ${shots.length} shots delivered with nothing found against them.`);
  }
  if (unexamined > 0) {
    parts.push(
      `${unexamined} ${unexamined === 1 ? "shot has" : "shots have"} not been examined yet.`,
    );
  }

  return parts.join(" ");
}
