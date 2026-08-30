import assert from "node:assert/strict";
import test from "node:test";

import { chooseProof, proofClaim, type ProofShot } from "./proof.ts";

const shot = (id: string, done: number, total: number, diag: object | null = null): ProofShot => ({
  id,
  frames_done: done,
  frames_total: total,
  diagnosis: diag as Record<string, unknown> | null,
});

test("prefers a finished shot that still has a diagnosis", () => {
  // The thesis in one card: every frame present, and something still wrong.
  const picked = chooseProof([
    shot("clean", 4, 4),
    shot("running-diagnosed", 17, 40, { cause: "x" }),
    shot("finished-diagnosed", 3, 3, { cause: "y" }),
  ]);
  assert.equal(picked.contrast, true);
  assert.deepEqual(picked.shots.map((s) => s.id), ["clean", "finished-diagnosed"]);
});

test("falls back to an unfinished diagnosed shot rather than showing none", () => {
  // The regression: filtering the diagnosed half by "finished" dropped the only
  // diagnosed shot on the farm and rendered one card under a promise of two.
  const picked = chooseProof([shot("clean", 4, 4), shot("running", 17, 40, { cause: "x" })]);
  assert.equal(picked.contrast, true);
  assert.deepEqual(picked.shots.map((s) => s.id), ["clean", "running"]);
});

test("does not claim a contrast it is not showing", () => {
  const picked = chooseProof([shot("a", 4, 4), shot("b", 3, 3)]);
  assert.equal(picked.contrast, false, "two undiagnosed shots are not opposite verdicts");
});

test("an empty farm yields nothing rather than a placeholder", () => {
  const picked = chooseProof([]);
  assert.deepEqual(picked.shots, []);
  assert.equal(picked.contrast, false);
});

test("the claim is derived from what is shown, never asserted over it", () => {
  assert.match(proofClaim(true, 2), /opposite verdicts/);
  assert.doesNotMatch(proofClaim(false, 2), /opposite verdicts/);
  assert.match(proofClaim(false, 2), /Nothing here is seeded/);
  assert.match(proofClaim(false, 0), /empty farm/);
});
