import assert from "node:assert/strict";
import test from "node:test";

import { buttonLabel, cooldownNote, diagnoseUrl, statusLine } from "./diagnose-state.ts";

test("the diagnose url targets the shot's own endpoint", () => {
  assert.equal(
    diagnoseUrl("https://api.example.invalid", "dailies:SEQ01:SH050:job-7"),
    "https://api.example.invalid/api/shots/dailies%3ASEQ01%3ASH050%3Ajob-7/diagnose",
  );
});

test("a trailing slash on the base does not double up", () => {
  assert.equal(
    diagnoseUrl("https://api.example.invalid/", "SH050"),
    "https://api.example.invalid/api/shots/SH050/diagnose",
  );
});

test("the button offers Diagnose first and Re-run once there is an answer", () => {
  assert.equal(buttonLabel("idle", false), "Diagnose");
  assert.equal(buttonLabel("idle", true), "Re-run");
});

test("a running investigation says so rather than going quiet", () => {
  assert.equal(buttonLabel("running", false), "Investigating...");
  // The wait is 20-50s against a live model. Silence for that long reads as a hang and
  // gets the button pressed again.
  assert.match(statusLine("running", null) ?? "", /30s/);
  assert.match(statusLine("running", null) ?? "", /Grafana MCP/);
});

test("a failure offers a retry and shows why", () => {
  assert.equal(buttonLabel("error", false), "Retry");
  assert.equal(statusLine("error", "502 from the API"), "502 from the API");
});

test("an idle button says nothing extra", () => {
  assert.equal(statusLine("idle", null), null);
});

test("a cached answer is explained rather than looking like nothing happened", () => {
  // The regression: pressing Re-run inside the cooldown returned the previous answer
  // silently, so the page did not change and the button read as broken.
  assert.equal(cooldownNote(20), "Answered just now. Ask again in 5m.");
  assert.equal(cooldownNote(180), "Answered 3m ago. Ask again in 2m.");
});

test("once the cooldown has passed it does not tell anyone to wait", () => {
  assert.equal(cooldownNote(300), "Answered 5m ago.");
  assert.equal(cooldownNote(9000), "Answered 150m ago.");
});
