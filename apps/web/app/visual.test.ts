import assert from "node:assert/strict";
import test from "node:test";

import { agreement, agreementNote, normalizeVisual, verdictLabel } from "./visual.ts";

test("a verdict without an observation is refused", () => {
  // The board must never render a judgement nobody can check against the frame.
  assert.equal(normalizeVisual({ verdict: "suspect" }), null);
  assert.equal(normalizeVisual({ verdict: "suspect", observation: "   " }), null);
});

test("an unknown verdict is refused rather than shown", () => {
  assert.equal(normalizeVisual({ verdict: "probably fine", observation: "a cube" }), null);
});

test("a usable verdict keeps the frame it judged", () => {
  const v = normalizeVisual({
    verdict: "suspect",
    observation: "  a flat magenta cube  ",
    confidence: "high",
    frame: "SH201/frame_0001.png",
  });
  assert.equal(v?.verdict, "suspect");
  assert.equal(v?.observation, "a flat magenta cube");
  assert.equal(v?.frame, "SH201/frame_0001.png", "so a person can open the same image");
});

test("one source speaking alone has no relationship to report", () => {
  const v = normalizeVisual({ verdict: "suspect", observation: "magenta" });
  assert.equal(agreement(null, v), null, "the investigator did not say what it found");
  assert.equal(agreement(true, null), null, "nobody looked at the frame");
});

test("a clean shot with a clean frame is agreement, not a disagreement", () => {
  // The bug this replaced. The board treated "a diagnosis exists" as "the telemetry
  // found a problem", so a healthy shot with a healthy frame was announced in yellow as
  // a DISAGREEMENT between sources. A diagnosis exists for every shot anyone asked
  // about, including the ones that turned out fine.
  const v = normalizeVisual({ verdict: "looks_correct", observation: "a grey cube" });
  assert.equal(agreement(false, v), "agree_clean");
  assert.match(agreementNote("agree_clean") ?? "", /nothing wrong/i);
});

test("a problem in both sources is agreement", () => {
  const v = normalizeVisual({ verdict: "suspect", observation: "magenta" });
  assert.equal(agreement(true, v), "agree_problem");
  assert.match(agreementNote("agree_problem") ?? "", /both sources agree/i);
});

test("a problem in only one source is the finding worth surfacing", () => {
  // The only outcome that tells a supervisor something neither check could have told
  // them alone, in either direction.
  const clean = normalizeVisual({ verdict: "looks_correct", observation: "a grey cube" });
  const wrong = normalizeVisual({ verdict: "suspect", observation: "magenta" });
  assert.equal(agreement(true, clean), "disagree", "telemetry saw it, the picture did not");
  assert.equal(agreement(false, wrong), "disagree", "the picture saw it, telemetry did not");
  assert.match(agreementNote("disagree") ?? "", /human eye/i);
});

test("verdicts read as words, not enum values", () => {
  assert.equal(verdictLabel("looks_correct"), "Looks correct");
  assert.equal(verdictLabel("broken"), "Broken");
});
