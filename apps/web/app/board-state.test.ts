import assert from "node:assert/strict";
import test from "node:test";

import { emptyMessage, stalenessNotice } from "./board-state.ts";

test("an idle farm is described as an idle farm", () => {
  assert.match(emptyMessage(true), /No shots are being watched yet/);
  assert.match(emptyMessage(true), /fills itself/);
});

test("an unreadable telemetry source is not described as an idle farm", () => {
  // The bug this exists for. A transient 503 from Grafana emptied the live board while
  // three renders sat in Prometheus, and the page said the farm had nothing to show. That
  // is a lie told to the one person who could act on it.
  const message = emptyMessage(false);
  assert.doesNotMatch(message, /No shots are being watched yet/);
  assert.match(message, /telemetry/i);
  assert.doesNotMatch(message, /fills itself/, "must not imply the farm is merely idle");
});

test("the unreadable message says the board cannot tell, not that nothing is wrong", () => {
  const message = emptyMessage(false);
  assert.match(message, /cannot|could not|unavailable/i);
});

test("a healthy board with rows says nothing extra", () => {
  assert.equal(stalenessNotice(true, true), null);
});

test("rows shown while telemetry is unreadable are labelled as possibly stale", () => {
  // Serving the last known state is right; serving it as if it were current is not. A
  // supervisor mid-incident needs both the rows and the warning.
  const notice = stalenessNotice(true, false);
  assert.ok(notice);
  assert.match(notice, /stale|last known|could not be refreshed/i);
});

test("an empty board handles its own message and needs no second one", () => {
  // Otherwise the page stacks "telemetry unavailable" on top of "telemetry unavailable".
  assert.equal(stalenessNotice(false, false), null);
});
