"use client";

/**
 * The one interactive thing on the board: ask the investigator about this shot.
 *
 * Without it the board is a table a judge can read and nothing they can do, and the
 * agent, which is the entire project, is reachable only with curl. It is deliberately
 * the *only* control on the page. A supervisor's question is "what is wrong with this
 * shot", and every other affordance would compete with the answer to it.
 *
 * The request is fired from the browser rather than through a server action so that the
 * board is exercising the same public API a judge can hit themselves, over CORS, with
 * nothing privileged in between. What they can see happening is what is actually
 * happening.
 */

import { useRouter } from "next/navigation";
import { useState, useTransition } from "react";

import { buttonLabel, diagnoseUrl, statusLine, type DiagnoseState } from "./diagnose-state";

type Props = {
  apiBase: string;
  shotId: string;
  hasDiagnosis: boolean;
};

export function DiagnoseButton({ apiBase, shotId, hasDiagnosis }: Props) {
  const [state, setState] = useState<DiagnoseState>("idle");
  const [error, setError] = useState<string | null>(null);
  const router = useRouter();
  // The POST resolving is not the end of the job: the board still has to re-read the
  // server component to show the answer. isPending keeps the button in its running state
  // across that refresh, so it never flicks back to "Diagnose" with nothing new on screen.
  const [isPending, startTransition] = useTransition();

  const running = state === "running" || isPending;

  async function run() {
    setState("running");
    setError(null);
    try {
      const response = await fetch(diagnoseUrl(apiBase, shotId), {
        method: "POST",
        // Generous, and matched to the work rather than to habit: the investigation runs
        // a live model over several Grafana queries, and a 429 from Vertex is retried
        // with backoff on the server side. Anything tighter aborts an investigation that
        // was going to succeed.
        signal: AbortSignal.timeout(180000),
      });
      if (!response.ok) {
        // The API answers 502 with a sentence written for a person; 404 and 503 likewise.
        // Show it rather than a status code: "could not reach the telemetry MCP server"
        // tells a supervisor this is not a finding about their shot, which a bare 502
        // does not.
        const body = await response.json().catch(() => null);
        const detail = body && typeof body.detail === "string" ? body.detail : null;
        setState("error");
        setError(detail ?? `The API answered ${response.status}.`);
        return;
      }
      setState("idle");
      startTransition(() => router.refresh());
    } catch (cause) {
      setState("error");
      setError((cause as Error).message);
    }
  }

  const status = statusLine(running ? "running" : state, error);

  return (
    <div className="diagnose">
      <button type="button" onClick={run} disabled={running} aria-busy={running}>
        {buttonLabel(running ? "running" : state, hasDiagnosis)}
      </button>
      {status ? (
        <span className={state === "error" && !running ? "diagnose-status error" : "diagnose-status"}>
          {status}
        </span>
      ) : null}
    </div>
  );
}
