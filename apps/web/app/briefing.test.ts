import assert from "node:assert/strict";
import test from "node:test";

import { briefing } from "./briefing.ts";
import type { Shot } from "./shots.ts";

const shot = (over: Partial<Shot> & { id: string }): Shot => ({
  frames_total: 4,
  frames_done: 4,
  risk: "DELIVERED",
  diagnosis: null,
  ...over,
});

// A CLEAN shot is one that was examined and had nothing found against it. A shot with no
// diagnosis is unexamined, which the briefing must never describe as healthy, so the two
// need different helpers. The first draft of this file used one for both and asserted the
// briefing would call an undiagnosed shot delivered.
const clean = (id: string) =>
  shot({
    id,
    diagnosis: { problem_found: false, cause: "No failures recorded.", evidence: [{ query: "q", finding: "f" }] },
  });
const unexamined = (id: string) => shot({ id, diagnosis: null });
const broken = (id: string, cause: string) =>
  shot({
    id,
    diagnosis: { problem_found: true, cause, evidence: [{ query: "q", finding: "f" }] },
  });

test("an empty farm is described in one honest line", () => {
  const text = briefing([]);
  assert.match(text, /nothing|no shots/i);
  assert.doesNotMatch(text, /\d+ shots? are delivering/i, "must not narrate a farm that is not there");
});

test("a clean farm says so without inventing a problem", () => {
  const text = briefing([clean("dailies:SEQ01:SH200:vqa-good"), clean("dailies:SEQ01:SH205:seq-b")]);
  assert.match(text, /2 shots/);
  assert.doesNotMatch(text, /wrong|missing|failed/i);
});

test("a found problem is named, with the shot and the cause", () => {
  // The whole point of the briefing: a producer should not have to read a grid to learn
  // that one deliverable is broken.
  const text = briefing([
    clean("dailies:SEQ01:SH200:vqa-good"),
    broken("dailies:SEQ01:SH201:vqa-bad", "A required asset was missing: /assets/jacket_diffuse.exr"),
  ]);
  assert.match(text, /SH201/);
  assert.match(text, /jacket_diffuse/);
});

test("the exception leads, because that is what a producer is scanning for", () => {
  const text = briefing([
    clean("dailies:SEQ01:SH200:vqa-good"),
    clean("dailies:SEQ01:SH205:seq-b"),
    broken("dailies:SEQ01:SH201:vqa-bad", "the jacket texture failed to resolve"),
  ]);
  assert.ok(text.indexOf("SH201") < text.indexOf("delivered"), "the problem must come first");
});

test("a shot nobody has diagnosed is never described as healthy", () => {
  // Absence of a diagnosis is not evidence of a clean frame. This is the same rule the
  // agent is held to, applied to the sentence a human actually reads.
  const text = briefing([unexamined("dailies:SEQ01:SH300:unknown")]);
  assert.doesNotMatch(text, /no problems|all clean|nothing wrong/i);
  assert.match(text, /not been|unexamined|no diagnosis|yet/i);
});

test("a late delivery is reported as a fact, not a forecast", () => {
  const text = briefing([
    shot({ id: "dailies:SEQ01:SH070:job-a", risk: "LATE", slack_seconds: -3600 }),
  ]);
  assert.match(text, /late/i);
});

test("an unfinished shot that will miss is reported as a forecast", () => {
  const text = briefing([
    shot({
      id: "dailies:SEQ01:SH100:job-b",
      risk: "MISSED",
      frames_done: 17,
      frames_total: 40,
      slack_seconds: -600,
    }),
  ]);
  assert.match(text, /SH100/);
  assert.match(text, /17 of 40|17\/40/);
});

test("it never runs past a few sentences", () => {
  // A briefing nobody finishes reading is a grid with extra steps.
  const many = Array.from({ length: 12 }, (_, i) => broken(`dailies:SEQ01:SH${300 + i}:j`, "a fault"));
  assert.ok(briefing(many).length < 420, "must summarise rather than enumerate");
});
