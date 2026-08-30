import assert from "node:assert/strict";
import test from "node:test";

import { describeConfidence, describeSlack, formatEta, humanDuration } from "./delivery.ts";

test("durations read the way a person says them", () => {
  assert.equal(humanDuration(30), "30s");
  assert.equal(humanDuration(45 * 60), "45m");
  assert.equal(humanDuration(2 * 3600 + 14 * 60), "2h 14m");
  assert.equal(humanDuration(3 * 3600), "3h");
  assert.equal(humanDuration(26 * 3600), "1d 2h");
});

test("slack says which side of the deadline the shot is on", () => {
  assert.equal(describeSlack(2 * 3600), "2h spare");
  assert.equal(describeSlack(-45 * 60), "45m late");
});

test("no slack means nothing is said, not zero", () => {
  // A row reading "0m spare" when nobody estimated anything is a claim, and a wrong one.
  assert.equal(describeSlack(null), null);
  assert.equal(describeSlack(undefined), null);
});

test("a confident estimate is not annotated", () => {
  // Annotating the confident case trains the eye to skip the annotation, which is
  // exactly when the "low" that matters stops being read.
  assert.equal(describeConfidence("high"), null);
  assert.equal(describeConfidence("unknown"), null);
  assert.equal(describeConfidence("medium"), "rough estimate");
  assert.equal(describeConfidence("low"), "low-confidence estimate");
});

test("an absent eta formats to nothing rather than the epoch", () => {
  assert.equal(formatEta(null), null);
  assert.equal(formatEta(undefined), null);
  assert.match(formatEta(1788100000) ?? "", /\d/);
});
