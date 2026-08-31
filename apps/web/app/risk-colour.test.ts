/**
 * Every risk state must have a colour someone chose.
 *
 * The board renders `<div className={`status s-${shot.risk}`}>`, and `.status b` defaults
 * to green. A risk member with no `.status.s-<NAME>` rule therefore does not fail, does
 * not warn, and does not look broken: it renders as calm green on the surface a
 * supervisor scans fastest. That is the failure mode this repo keeps finding, a wrong
 * configuration producing a plausible result rather than an error, and here it would
 * paint a missed deadline the same colour as a delivered shot.
 *
 * The names are duplicated from `dailies_api.state.Risk` because CSS cannot import a
 * Python enum. `test_risk_has_exactly_the_members_the_board_styles` pins the same set on
 * the API side, so a member added there and forgotten here fails one of the two.
 */
import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";

const CSS = readFileSync(join(import.meta.dirname, "globals.css"), "utf8");

/** Green is the default in `.status b`, and these two are the states that want it. */
const DEFAULT_GREEN = ["DELIVERED", "ON_TRACK"];

/** Everything that must be visibly not-green, and therefore needs its own rule. */
const NEEDS_A_RULE = ["WATCH", "AT_RISK", "LATE", "CRITICAL", "MISSED"];

test("every elevated risk state has its own colour rule", () => {
  for (const name of NEEDS_A_RULE) {
    assert.ok(
      CSS.includes(`.status.s-${name} b`),
      `${name} has no .status.s-${name} b rule, so it renders as the default green`,
    );
  }
});

test("the calm states are left on the default deliberately", () => {
  for (const name of DEFAULT_GREEN) {
    assert.ok(
      !CSS.includes(`.status.s-${name} b`),
      `${name} now has its own rule; if that is intended, move it out of DEFAULT_GREEN`,
    );
  }
});

test("LATE is amber, not the red spent on unfinished work", () => {
  // A finished shot cannot be acted on, so it must not carry the same visual weight as a
  // rejected frame or a deadline still being missed.
  const rule = CSS.slice(CSS.indexOf(".status.s-LATE b"));
  assert.match(rule.slice(0, 60), /var\(--at-risk\)/);
});
