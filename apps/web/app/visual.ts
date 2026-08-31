/**
 * Reading the visual verdict, and saying whether the two sources agree.
 *
 * The board shows two independent answers about one shot: what the telemetry said, and
 * what the frame looks like. Putting them side by side is not enough. A supervisor
 * scanning at 2am will not diff two paragraphs, and the case worth their attention is
 * precisely the one where the sources disagree - a picture that looks fine beside a log
 * saying an asset was missing, or a clean telemetry record beside a magenta frame.
 *
 * So the relationship is computed and stated. That is the whole reason for running two
 * checks rather than one.
 */

export type VisualVerdict = "looks_correct" | "suspect" | "broken";

export type Visual = {
  verdict: VisualVerdict;
  observation: string;
  confidence: string | null;
  frame: string | null;
};

/** Parse the API's visual field, refusing anything that is not a usable verdict. */
export function normalizeVisual(raw: unknown): Visual | null {
  if (raw === null || typeof raw !== "object") return null;
  const v = raw as Record<string, unknown>;
  const verdict = v.verdict;
  const observation = v.observation;
  // Both required. A verdict with no observation is the unverifiable output this project
  // argues against, and the API already refuses one; this is the second gate, because
  // the board must never render a judgement nobody can check against the frame.
  if (verdict !== "looks_correct" && verdict !== "suspect" && verdict !== "broken") return null;
  if (typeof observation !== "string" || observation.trim() === "") return null;
  return {
    verdict,
    observation: observation.trim(),
    confidence: typeof v.confidence === "string" ? v.confidence : null,
    frame: typeof v.frame === "string" ? v.frame : null,
  };
}

export type Agreement = "agree_problem" | "agree_clean" | "disagree" | null;

/**
 * How the two sources relate, or `null` when only one of them spoke.
 *
 * `disagree` is the finding. It means one source sees a problem the other does not, and
 * it is the only outcome here that tells a supervisor something neither check could have
 * told them alone.
 */
export function agreement(hasDiagnosis: boolean, visual: Visual | null): Agreement {
  if (!visual || !hasDiagnosis) return null;
  const visualUnhappy = visual.verdict !== "looks_correct";
  // The telemetry side is coarse on purpose: the investigator is only asked when someone
  // suspects a problem, so a diagnosis existing is not itself a claim that one was found.
  // What can be compared honestly is whether the FRAME looks wrong, against whether the
  // diagnosis reported a cause.
  return visualUnhappy ? "agree_problem" : "disagree";
}

/** One sentence naming the relationship, for the reader who will not diff two paragraphs. */
export function agreementNote(state: Agreement): string | null {
  if (state === "agree_problem") {
    return "Both sources agree: the telemetry found a cause and the frame looks wrong.";
  }
  if (state === "disagree") {
    return "The sources disagree. The telemetry reported a cause, and the frame looks correct. Worth a human eye on both before acting.";
  }
  return null;
}

/** The human label for a verdict. */
export function verdictLabel(verdict: VisualVerdict): string {
  if (verdict === "looks_correct") return "Looks correct";
  if (verdict === "suspect") return "Suspect";
  return "Broken";
}
