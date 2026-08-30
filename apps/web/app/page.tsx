/**
 * The case for Dailies.
 *
 * The board on its own is an instrument for someone who already has the context. A
 * first-time reader opening the hosted URL saw shot ids and status words with nothing
 * saying what the product is, or why "3 of 3 frames, on track" beside a missing-asset
 * finding is the entire point. This page makes the argument, and then hands over to the
 * instrument.
 *
 * It reads the SAME live API the board does, so the proof section shows real shots in
 * their real state. That is the difference between a product page and a marketing page:
 * if the farm is quiet, this page says so rather than showing an illustration of a farm
 * that is busy.
 */

import Link from "next/link";

import { chooseProof, proofClaim } from "./proof";
import { fetchShots, hasLanded, parseId, percent, type Shot } from "./shots";

export const dynamic = "force-dynamic";

/** The industry describing this failure in its own words, with attribution. */
const VOICES = [
  {
    quote:
      "The material will fail to load silently, rendering as black. The render completes without error messages, but the output is incorrect.",
    who: "on third-party materials missing from farm workers",
  },
  {
    quote:
      "Even if only one texture is missing, the render will submit a 'done' status. You discover it after the render completes, and you have already paid for those broken frames.",
    who: "iRender, on missing textures",
  },
  {
    quote: "Textures are a silent cause of instability.",
    who: "MotionMedia, on why motion-graphics renders fail",
  },
];

const CHAIN = [
  {
    step: "Blender renders",
    detail:
      "A real render runs as a Cloud Run job. Its stdout is parsed into typed events by the same parser the tests cover heaviest.",
  },
  {
    step: "OpenTelemetry ships it",
    detail:
      "Frame durations, memory and job progress go to Prometheus. Render-domain failures go to Loki, because a missing asset is a sentence, not a number.",
  },
  {
    step: "Grafana holds the truth",
    detail:
      "The board reconstructs every shot from those series. There is no database of shots to drift from the farm, and nothing to seed.",
  },
  {
    step: "The agent investigates",
    detail:
      "A Gemini agent on the Agent Development Kit reaches Prometheus and Loki only through the Grafana MCP server. Remove that connection and the project stops rather than degrades.",
  },
];

function ProofCard({ shot }: { shot: Shot }) {
  const { shot: label } = parseId(shot.id);
  const missed = shot.risk === "MISSED" || shot.risk === "CRITICAL";
  const landed = hasLanded(shot);
  return (
    <article className={missed ? "card miss" : "card"}>
      <div className="slate">
        <span className="k">SCENE</span>
        <span className="v">{label}</span>
        <span className="k">STATE</span>
        <span className="v">{landed ? "DELIVERED" : "RUNNING"}</span>
      </div>
      <div className="body">
        <div className="frames">
          {shot.frames_done}
          <span>/{shot.frames_total}</span>
        </div>
        <div className="track" aria-hidden="true">
          <i style={{ width: `${percent(shot.frames_done, shot.frames_total)}%` }} />
        </div>
        <div className={`status s-${shot.risk}`}>
          <b>{shot.risk.replace("_", " ")}</b>
          <em>{shot.diagnosis ? "diagnosed" : "not yet asked"}</em>
        </div>
      </div>
    </article>
  );
}

export default async function Landing() {
  const { shots, error } = await fetchShots();

  // Which shots make the argument, and whether the argument may be made at all. The
  // claim is derived from the selection rather than written above it: a page whose whole
  // thesis is that a claim must be earned cannot promise a contrast it is not showing.
  const { shots: shown, contrast } = chooseProof(shots);

  return (
    <main className="wrap landing">
      <div className="mast">
        <span className="wordmark">Dailies</span>
        <div className="stripe" aria-hidden="true" />
        <Link className="to-board" href="/board">
          Open the board
        </Link>
      </div>

      <section className="hero">
        <h1>
          A render farm knows how to handle a crash.
          <br />
          It has no idea what to do with a frame that <em>worked</em>.
        </h1>
        <p className="lede">
          A texture fails to resolve on the worker. Blender falls back to a default material,
          prints a warning, and <strong>exits 0</strong>. The frame saves. The frame count is
          complete. The durations look normal. Every automated system in the pipeline calls it a
          success, and the jacket is grey.
        </p>
        <p className="lede">
          Nobody finds out until a human watches the dailies. This is the failure Dailies catches,
          from telemetry, before anyone watches anything.
        </p>
      </section>

      <section className="voices" aria-label="How the industry describes this failure">
        <h2 className="eyebrow">Not a hypothetical</h2>
        <div className="voice-grid">
          {VOICES.map((v) => (
            <figure key={v.who}>
              <blockquote>{v.quote}</blockquote>
              <figcaption>{v.who}</figcaption>
            </figure>
          ))}
        </div>
      </section>

      <section className="proof" aria-label="Live proof">
        <h2 className="eyebrow">Live, from the farm, right now</h2>
        <p className="proof-lede">{proofClaim(contrast, shown.length)}</p>
        {error ? (
          <p className="error">
            The farm is not answering.
            <code>{error}</code>
          </p>
        ) : shown.length === 0 ? (
          <p className="empty">No renders in the last 24 hours. The board fills itself when one runs.</p>
        ) : (
          <div className="grid proof-grid">
            {shown.map((s) => (
              <ProofCard key={s.id} shot={s} />
            ))}
          </div>
        )}
        <p className="proof-note">
          The case that matters is a shot whose frames are <strong>all complete</strong> and which
          is still wrong. Every number a scheduler looks at says success. The only evidence is a
          log line, which is why the agent reads logs as well as metrics, and why every finding it
          reports has to name the query behind it.
        </p>
        <Link className="cta" href="/board">
          Open the board and press Diagnose
        </Link>
      </section>

      <section className="chain" aria-label="How it works">
        <h2 className="eyebrow">How it works</h2>
        <ol className="chain-list">
          {CHAIN.map((c, i) => (
            <li key={c.step}>
              <span className="n" aria-hidden="true">
                {String(i + 1).padStart(2, "0")}
              </span>
              <div>
                <h3>{c.step}</h3>
                <p>{c.detail}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      <section className="cost" aria-label="What a diagnosis costs">
        <h2 className="eyebrow">What one diagnosis costs</h2>
        <p className="cost-lede">
          The agent is measured the way the farm is. If you cannot trust what you cannot see, that
          has to apply to the thing doing the looking.
        </p>
        <dl className="cost-grid">
          <div>
            <dt>Wall clock</dt>
            <dd>
              40.3<span>s</span>
            </dd>
          </div>
          <div>
            <dt>Model turns</dt>
            <dd>7</dd>
          </div>
          <div>
            <dt>Input tokens</dt>
            <dd>27,313</dd>
          </div>
          <div>
            <dt>Thinking tokens</dt>
            <dd>5,124</dd>
          </div>
          <div>
            <dt>Output tokens</dt>
            <dd>878</dd>
          </div>
        </dl>
        <p className="cost-note">
          Measured on a real investigation, in OpenTelemetry GenAI conventions, in the same Grafana
          stack the renders report to. Thinking is counted separately rather than folded into
          output: it is billed, it appears in neither the prompt nor the answer, and it is six times
          the size of the reply. Folded in, this would look like an 878-token call.
        </p>
      </section>

      <footer className="foot">
        <Link className="cta" href="/board">
          Open the board
        </Link>
        <p>
          Built on Google Cloud with Gemini and the Agent Development Kit, reading Grafana Cloud
          through the Grafana MCP server.
        </p>
      </footer>
    </main>
  );
}
