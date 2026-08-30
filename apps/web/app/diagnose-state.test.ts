import assert from "node:assert/strict";
import test from "node:test";

import { buttonLabel, diagnoseUrl, statusLine } from "./diagnose-state.ts";

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
