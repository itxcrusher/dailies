import { strict as assert } from "node:assert";
import test from "node:test";

import { answeredAgo, provenanceNote } from "./answer-age.ts";

const NOW = 1_700_000_000;

test("an answer nobody has produced has no age", () => {
  assert.equal(answeredAgo(null, NOW), null);
  assert.equal(answeredAgo(undefined, NOW), null);
});

test("a very recent answer reads as just now", () => {
  // Under a minute, "0m ago" is both uglier and less true than "just now".
  assert.equal(answeredAgo(NOW - 5, NOW), "just now");
  assert.equal(answeredAgo(NOW - 59, NOW), "just now");
});

test("minutes, hours and days each get their own unit", () => {
  assert.equal(answeredAgo(NOW - 60, NOW), "1m ago");
  assert.equal(answeredAgo(NOW - 45 * 60, NOW), "45m ago");
  assert.equal(answeredAgo(NOW - 3 * 3600, NOW), "3h ago");
  assert.equal(answeredAgo(NOW - 47 * 3600, NOW), "47h ago");
  assert.equal(answeredAgo(NOW - 72 * 3600, NOW), "3d ago");
});

test("a timestamp in the future reads as just now rather than negative", () => {
  // Two clocks are involved: the API stamps with its own, the browser renders with the
  // viewer's. A few seconds of skew must not produce "-1m ago", which reads as a bug in
  // the thing the board is asking to be trusted.
  assert.equal(answeredAgo(NOW + 30, NOW), "just now");
});

test("a current answer needs no provenance note", () => {
  assert.equal(provenanceNote(false), null);
});

test("an answer from a superseded agent says so", () => {
  // The SH200 case: the answer was real, the agent that produced it is gone, and the
  // board must not present its conclusion as one the current agent stands behind.
  assert.equal(provenanceNote(true), "from an earlier agent");
});
