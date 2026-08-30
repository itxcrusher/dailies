/**
 * Choosing which real shots the case page shows as proof, and what it may claim.
 *
 * The section's argument is "same agent, same tools, opposite verdicts", and that is only
 * true when a clean shot and a diagnosed one are both on screen. The first version
 * asserted it unconditionally and then rendered whatever it found, which on a farm with
 * one diagnosed shot meant a single card under a sentence promising two.
 *
 * A page that overstates what it is showing is a strange thing to build into a product
 * whose entire argument is that a claim must be earned. So the claim is derived from the
 * selection rather than written above it.
 */

export type ProofShot = {
  id: string;
  frames_total: number;
  frames_done: number;
  diagnosis: Record<string, unknown> | null;
};

export type Proof<T> = {
  shots: T[];
  /** True only when a clean shot and a diagnosed one are both being shown. */
  contrast: boolean;
};

function landed(s: ProofShot): boolean {
  return s.frames_total > 0 && s.frames_done >= s.frames_total;
}

/**
 * Up to two shots, chosen to make the argument if the farm allows it.
 *
 * Preference order for the diagnosed half:
 *   1. finished AND diagnosed - the whole thesis in one card, every frame present and
 *      something still wrong;
 *   2. any diagnosed shot - still shows the agent has spoken, just not the sharp case;
 * and for the other half, a finished shot with no diagnosis, so the pair contrasts.
 */
export function chooseProof<T extends ProofShot>(shots: readonly T[]): Proof<T> {
  const diagnosed = shots.filter((s) => s.diagnosis !== null);
  const wrong = diagnosed.find(landed) ?? diagnosed[0];
  const clean = shots.find((s) => s.diagnosis === null && landed(s) && s.id !== wrong?.id);

  const picked = [clean, wrong].filter(Boolean) as T[];
  if (picked.length === 2) {
    return { shots: picked, contrast: true };
  }
  // Not enough to contrast. Show what exists rather than nothing, and do not claim it.
  return { shots: (picked.length ? picked : shots.slice(0, 2)) as T[], contrast: false };
}

/** What the section may honestly say above the cards it is about to render. */
export function proofClaim(contrast: boolean, count: number): string {
  if (contrast) {
    return "Real shots on the deployed system, read from Prometheus as this page loaded. Same agent, same tools, opposite verdicts.";
  }
  if (count > 0) {
    return "Real shots on the deployed system, read from Prometheus as this page loaded. Nothing here is seeded or illustrated.";
  }
  return "Nothing has rendered in the last 24 hours, so there is nothing to show. This page reads the farm rather than a fixture, so an empty farm is an empty section.";
}
