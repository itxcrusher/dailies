/**
 * Reading the live board state, shared by the case page and the board itself.
 *
 * Extracted when `/` was added, so the two pages cannot disagree about what is happening
 * on the farm. The case page's proof section shows real shots; if it fetched them its own
 * way, the argument on `/` could drift from the instrument on `/board` and a judge would
 * be reading two different truths on the same deployment.
 */

// DELIVERED and LATE record what happened to a FINISHED shot; the rest forecast an
// unfinished one. Mixing the two put a green ON TRACK pill above "delivered 22h late".
export type Risk =
  | "DELIVERED"
  | "ON_TRACK"
  | "WATCH"
  | "AT_RISK"
  | "LATE"
  | "CRITICAL"
  | "MISSED";

export type Shot = {
  id: string;
  frames_total: number;
  frames_done: number;
  risk: Risk;
  diagnosis: Record<string, unknown> | null;
  // What Gemini saw when it looked at this shot's newest frame, or null if nobody has
  // looked. Beside the diagnosis rather than inside it: two independent sources, and the
  // case worth a supervisor's attention is when they disagree.
  visual?: Record<string, unknown> | null;
  // All nullable, and null is meaningful rather than missing: a shot that has rendered
  // no frames has no ETA to give, and one with no promised date has no slack. Rendering
  // a zero for either would be inventing a claim.
  // When the answer beside this shot was produced, and whether the agent running now is
  // the one that produced it. Without these the board presents a stored conclusion as a
  // statement about the present, which is how a superseded verdict sat here for days.
  answered_at?: number | null;
  answer_stale?: boolean;
  eta_epoch?: number | null;
  deadline_epoch?: number | null;
  slack_seconds?: number | null;
  confidence?: string | null;
};

/**
 * Where the API lives.
 *
 * `DAILIES_API_URL` is what Terraform injects into the Cloud Run service and is read at
 * request time, which is the only thing that works: the API's URL is not known until its
 * service exists, so it cannot be a build argument. `NEXT_PUBLIC_API_URL` is the
 * local-development spelling and is inlined at build time.
 */
export function apiBase(): string {
  const raw =
    process.env.DAILIES_API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8080";
  return raw.replace(/\/+$/, "");
}

export async function fetchShots(): Promise<{
  shots: Shot[];
  error: string | null;
  telemetryReadable: boolean;
}> {
  const url = `${apiBase()}/api/shots`;
  try {
    const response = await fetch(url, {
      cache: "no-store",
      // 25s, set above the worst cold start rather than just above the warm one. A
      // screenshot of the deployed board taken cold once read "No shot data ... aborted
      // due to timeout" at 8s: the exact page a first-time visitor gets, and one that
      // looks identical to a broken deployment. Cloud Run now keeps a warm instance, so
      // this is the backstop for when that is not enough.
      signal: AbortSignal.timeout(25000),
    });
    if (!response.ok) {
      return { shots: [], error: `${url} answered ${response.status}`, telemetryReadable: true };
    }
    const body = (await response.json()) as { shots?: Shot[]; telemetry_readable?: boolean };
    // Absent means an older API that cannot answer, and anything that cannot answer must
    // not raise an alarm it has no evidence for.
    return { shots: body.shots ?? [], error: null, telemetryReadable: body.telemetry_readable ?? true };
  } catch (cause) {
    // The page still renders. A page that 500s when the API is down tells a judge
    // nothing; a page that says which URL failed tells them exactly where to look.
    return {
      shots: [],
      error: `${url} is unreachable (${(cause as Error).message})`,
      telemetryReadable: true,
    };
  }
}

export function percent(done: number, total: number): number {
  if (total <= 0) return 0;
  return Math.min(100, Math.round((done / total) * 100));
}

/** Whether a shot has finished, so its delivery line can be written in the past tense. */
export function hasLanded(shot: Shot): boolean {
  return shot.frames_total > 0 && shot.frames_done >= shot.frames_total;
}

/** `project:sequence:shot:render_job` split for display, or the raw id if it is not one. */
export function parseId(id: string): { seq: string; shot: string; job: string } {
  const parts = id.split(":");
  if (parts.length === 4) {
    return { seq: parts[1], shot: parts[2], job: parts[3] };
  }
  return { seq: "", shot: id, job: "" };
}
