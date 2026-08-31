/**
 * The board's boundary around an investigator diagnosis.
 *
 * `Shot.diagnosis` is an untyped mapping all the way from the API: `dailies_api.state`
 * holds it as `dict[str, Any]` on purpose, because the authority on its shape is the
 * agent's response schema and duplicating that in two places would give the project two
 * definitions to keep in step. The consequence lands here. By the time a diagnosis
 * reaches this page it is model output that has crossed two process boundaries, and a
 * server component that indexes into it blindly turns one malformed answer into a 500 on
 * the entire board: every other shot's row lost because one shot's evidence was a string.
 *
 * So nothing in the page reads a raw diagnosis. It reads what `normalizeDiagnosis`
 * returns, which is either something worth rendering or `null`.
 *
 * The rule the shape encodes: a diagnosis is worth showing when it says something. An
 * object with neither a cause nor a single piece of evidence is not a thin diagnosis, it
 * is an absent one, and rendering it would put an empty bordered box under a shot row
 * that a supervisor would read as "investigated, nothing found".
 */

/** The three answers `DIAGNOSIS_SCHEMA` allows. Anything else is not confidence. */
export type Confidence = "high" | "medium" | "low";

const CONFIDENCES: readonly string[] = ["high", "medium", "low"];

/**
 * One query and what it showed.
 *
 * Both halves are required by the schema, so an entry missing one has already broken its
 * contract. The empty string is kept rather than the entry dropped: showing a finding
 * with no query behind it lets a supervisor see that the claim is unsupported, which is
 * exactly the judgement this board exists to hand them.
 */
export type EvidenceEntry = {
  query: string;
  finding: string;
};

export type Diagnosis = {
  /**
   * Whether the investigator said it actually found something wrong.
   *
   * `null` when the answer did not say. Not defaulted either way: a diagnosis exists for
   * every shot anyone asked about, including healthy ones, so assuming false would put a
   * clean reading on an answer that never made one, and assuming true would redden a
   * shot nobody called broken. The board compares this against the visual verdict, and
   * it once inferred the value from "a diagnosis exists", which announced every healthy
   * shot as a disagreement between sources.
   */
  problemFound: boolean | null;
  cause: string;
  evidence: EvidenceEntry[];
  confidence: Confidence | null;
  affectedFrames: string | null;
  recommendedAction: string | null;
};

/** A trimmed string, or "" for anything that is not a usable string. */
function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

/** A trimmed string, or null. Numbers are accepted: a frame range is often written as one. */
function optionalText(value: unknown): string | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(value);
  }
  return text(value) || null;
}

function confidenceOf(value: unknown): Confidence | null {
  const named = text(value).toLowerCase();
  return CONFIDENCES.includes(named) ? (named as Confidence) : null;
}

function evidenceOf(value: unknown): EvidenceEntry[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const entries: EvidenceEntry[] = [];
  for (const raw of value) {
    if (typeof raw !== "object" || raw === null) {
      continue;
    }
    const item = raw as Record<string, unknown>;
    const entry = { query: text(item.query), finding: text(item.finding) };
    // An entry with neither half carries no information at all; it would render as a
    // numbered row of empty space, which reads as a rendering bug rather than a thin
    // answer.
    if (entry.query || entry.finding) {
      entries.push(entry);
    }
  }
  return entries;
}

/**
 * What the board should render for this diagnosis, or `null` for nothing at all.
 *
 * Total on `unknown`: every branch here is reachable from a real answer, because the
 * only thing standing between a Gemini response and this function is a JSON parse.
 */
export function normalizeDiagnosis(raw: unknown): Diagnosis | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    return null;
  }
  const source = raw as Record<string, unknown>;
  const diagnosis: Diagnosis = {
    problemFound: typeof source.problem_found === "boolean" ? source.problem_found : null,
    cause: text(source.cause),
    evidence: evidenceOf(source.evidence),
    confidence: confidenceOf(source.confidence),
    affectedFrames: optionalText(source.affected_frames),
    recommendedAction: optionalText(source.recommended_action),
  };
  // Confidence and a frame range with nothing to be confident *about* is not a diagnosis.
  if (!diagnosis.cause && diagnosis.evidence.length === 0) {
    return null;
  }
  return diagnosis;
}
