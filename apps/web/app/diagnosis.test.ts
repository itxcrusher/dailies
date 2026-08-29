/**
 * What the board is allowed to believe about a diagnosis.
 *
 * The API types `Shot.diagnosis` as an untyped mapping on purpose: the authority on its
 * shape is the agent's response schema, not the board. That makes every field on it
 * unverified data by the time it reaches this page, and a server component that indexes
 * blindly into it turns one malformed answer into a 500 on the whole board - every other
 * shot's row lost because one shot's diagnosis had a number where a string belonged.
 *
 * So the normaliser is the boundary, and this is where its behaviour is pinned.
 */

import assert from "node:assert/strict";
import { test } from "node:test";

import { normalizeDiagnosis } from "./diagnosis.ts";

test("a full diagnosis survives intact", () => {
  const result = normalizeDiagnosis({
    shot: "dailies:seq01:SH030:j4956",
    cause: "Frame 118 rendered without jacket_diffuse.exr",
    evidence: [
      {
        query: '{service_name="dailies-render", shot="SH030"} |= "Unable to open file"',
        finding: "WARN: Unable to open file '/assets/jacket_diffuse.exr'",
      },
      { query: "render_frame_duration_seconds_count", finding: "The job exited 0" },
    ],
    affected_frames: "118-124",
    recommended_action: "Re-render 118-124 once the asset is published",
    confidence: "high",
  });

  assert.ok(result);
  assert.equal(result.cause, "Frame 118 rendered without jacket_diffuse.exr");
  assert.equal(result.evidence.length, 2);
  assert.equal(result.evidence[1].finding, "The job exited 0");
  assert.equal(result.affectedFrames, "118-124");
  assert.equal(result.recommendedAction, "Re-render 118-124 once the asset is published");
  assert.equal(result.confidence, "high");
});

test("a shot with no diagnosis normalises to nothing to render", () => {
  assert.equal(normalizeDiagnosis(null), null);
  assert.equal(normalizeDiagnosis(undefined), null);
});

test("a diagnosis that is not an object is not a diagnosis", () => {
  // A board that trusted this would render "d", "i", "a"... as evidence entries.
  assert.equal(normalizeDiagnosis("diagnosis pending"), null);
  assert.equal(normalizeDiagnosis(["cause"]), null);
  assert.equal(normalizeDiagnosis(42), null);
});

test("an object with nothing sayable in it renders as no diagnosis, not an empty box", () => {
  assert.equal(normalizeDiagnosis({}), null);
  assert.equal(normalizeDiagnosis({ cause: "   ", evidence: [] }), null);
  assert.equal(normalizeDiagnosis({ confidence: "high" }), null);
});

test("evidence that is not a list is dropped rather than rendered", () => {
  const result = normalizeDiagnosis({ cause: "Out of memory on the worker", evidence: "lots" });
  assert.ok(result);
  assert.deepEqual(result.evidence, []);
});

test("an evidence entry keeps its half when the other half is missing", () => {
  // Both halves are required by DIAGNOSIS_SCHEMA, so a one-sided entry means the answer
  // already failed its contract. Showing the half that exists is still more honest than
  // dropping it: a supervisor can see the claim is thin.
  const result = normalizeDiagnosis({
    cause: "Asset missing",
    evidence: [
      { query: "up{job='dailies-render'}", finding: "" },
      { finding: "No such series" },
      { query: "   ", finding: "   " },
      "not an entry at all",
      null,
    ],
  });

  assert.ok(result);
  assert.equal(result.evidence.length, 2);
  assert.equal(result.evidence[0].query, "up{job='dailies-render'}");
  assert.equal(result.evidence[0].finding, "");
  assert.equal(result.evidence[1].query, "");
  assert.equal(result.evidence[1].finding, "No such series");
});

test("a diagnosis with evidence but no cause still shows its evidence", () => {
  const result = normalizeDiagnosis({
    evidence: [{ query: "render_worker_memory_bytes", finding: "Peaked at 31.4 GiB" }],
  });

  assert.ok(result);
  assert.equal(result.cause, "");
  assert.equal(result.evidence.length, 1);
});

test("a confidence outside the closed set is reported as unknown, not as itself", () => {
  // The schema pins high/medium/low. Anything else would be styled by a class name that
  // does not exist, which reads on the board as a confidence chip with no colour.
  assert.equal(normalizeDiagnosis({ cause: "x", confidence: "probably fine" })?.confidence, null);
  assert.equal(normalizeDiagnosis({ cause: "x", confidence: 0.9 })?.confidence, null);
  assert.equal(normalizeDiagnosis({ cause: "x", confidence: "HIGH " })?.confidence, "high");
});

test("affected frames survive whether the model wrote a string or a number", () => {
  assert.equal(normalizeDiagnosis({ cause: "x", affected_frames: "118-124" })?.affectedFrames, "118-124");
  assert.equal(normalizeDiagnosis({ cause: "x", affected_frames: 118 })?.affectedFrames, "118");
  assert.equal(normalizeDiagnosis({ cause: "x", affected_frames: "  " })?.affectedFrames, null);
  assert.equal(normalizeDiagnosis({ cause: "x" })?.affectedFrames, null);
});
