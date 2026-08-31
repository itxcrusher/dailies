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
export function agreement(
  telemetryFoundProblem: boolean | null,
  visual: Visual | null,
): Agreement {
  // Both sides must have actually spoken. `null` means the investigator did not say
  // whether it found a problem, and an answer that did not say is not one to compare
  // against - inferring it from the presence of a diagnosis is precisely the bug this
  // signature exists to prevent.
  if (!visual || telemetryFoundProblem === null) return null;

  const visualUnhappy = visual.verdict !== "looks_correct";
  if (telemetryFoundProblem && visualUnhappy) return "agree_problem";
  if (!telemetryFoundProblem && !visualUnhappy) return "agree_clean";
  return "disagree";
}

/** One sentence naming the relationship, for the reader who will not diff two paragraphs. */
export function agreementNote(state: Agreement): string | null {
  if (state === "agree_problem") {
    return "Both sources agree: the telemetry found a problem and the frame looks wrong.";
  }
  if (state === "agree_clean") {
    return "Both sources agree: nothing wrong in the telemetry, and the frame looks correct.";
  }
  if (state === "disagree") {
    return "The sources disagree. One found a problem the other did not. Worth a human eye on both before acting.";
  }
  return null;
}

/** The human label for a verdict. */
export function verdictLabel(verdict: VisualVerdict): string {
  if (verdict === "looks_correct") return "Looks correct";
  if (verdict === "suspect") return "Suspect";
  return "Broken";
}
