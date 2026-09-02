"""Breaking a render on purpose, from a terminal, in front of someone.

    python -m dailies_chaos list
    python -m dailies_chaos inject missing-texture --shot SH400
    python -m dailies_chaos inject missing-texture --shot SH400 --dry-run

**Shells out to gcloud rather than calling the Cloud Run API.** The API path would need
application-default credentials, which on a developer machine is a single global file
shared with every other project on it; a demo tool is not worth writing into that. gcloud
carries its own login, so this uses the operator's existing identity and adds nothing to
the machine.

The command is printed before it runs. This exists to be used while someone is watching,
and a tool that quietly does something to a farm is the wrong shape for that.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from .scenarios import SCENARIOS, Scenario, find

__all__ = ["build_command", "main"]

DEFAULT_JOB = "dailies-render"
DEFAULT_REGION = "us-central1"
DEFAULT_PROJECT = "dailies-render-2026"


def build_command(
    scenario: Scenario,
    *,
    shot: str,
    job: str = DEFAULT_JOB,
    region: str = DEFAULT_REGION,
    project: str = DEFAULT_PROJECT,
) -> list[str]:
    """The gcloud invocation that induces ``scenario`` on ``shot``.

    Pure, so the command can be asserted in a test without running anything. The render
    job's own definition supplies the image, the OTLP wiring and the bucket mount; only
    the fault and the shot are set here.
    """
    env = {
        "DAILIES_SHOT": shot,
        # A render job label that says out loud this was induced, so a shot from the chaos
        # suite is never mistaken later for something the farm did by itself.
        "DAILIES_RENDER_JOB": f"chaos-{scenario.name}",
        "DAILIES_FRAME_START": "1",
        "DAILIES_FRAME_END": str(scenario.frames),
        **scenario.env,
    }
    pairs = ",".join(f"{key}={value}" for key, value in env.items())
    return [
        "gcloud",
        "run",
        "jobs",
        "execute",
        job,
        f"--region={region}",
        f"--project={project}",
        f"--update-env-vars={pairs}",
    ]


def _listing() -> str:
    lines = ["", "  Scenarios this farm can be made to have on purpose:", ""]
    for scenario in SCENARIOS:
        lines.append(f"  {scenario.name}")
        lines.append(f"      {scenario.summary}")
        lines.append(f"      proves: {scenario.proves}")
        lines.append(f"      expect: {scenario.expect}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dailies_chaos", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="Show the scenarios and what each one proves.")

    inject = sub.add_parser("inject", help="Induce one fault on a fresh shot.")
    inject.add_argument("scenario", help="Scenario name, from `list`.")
    inject.add_argument("--shot", required=True, help="Shot id to render, e.g. SH400.")
    inject.add_argument("--job", default=DEFAULT_JOB)
    inject.add_argument("--region", default=DEFAULT_REGION)
    inject.add_argument("--project", default=DEFAULT_PROJECT)
    inject.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the command without running it.",
    )
    args = parser.parse_args(argv)

    if args.command == "list":
        print(_listing())
        return 0

    scenario = find(args.scenario)
    if scenario is None:
        known = ", ".join(s.name for s in SCENARIOS)
        print(f"No scenario called {args.scenario!r}. Known: {known}", file=sys.stderr)
        return 2

    command = build_command(
        scenario, shot=args.shot, job=args.job, region=args.region, project=args.project
    )
    print(f"\n  {scenario.name}: {scenario.summary}")
    print(f"  expect: {scenario.expect}\n")
    print("  " + " ".join(command) + "\n")
    if args.dry_run:
        return 0

    # Resolved to a full path, not passed as the bare name. On Windows gcloud is a .cmd
    # and subprocess does not apply PATHEXT, so ["gcloud", ...] raises FileNotFoundError
    # even though the tool is installed and on PATH. shutil.which does apply it, which
    # makes this both the existence check and the fix.
    executable = shutil.which("gcloud")
    if executable is None:
        # Named rather than left to a FileNotFoundError, which reads as a bug in this tool
        # rather than a missing prerequisite on the machine.
        print(
            "gcloud is not on PATH; install the Google Cloud SDK or use --dry-run.",
            file=sys.stderr,
        )
        return 2

    # argv, never a shell string: the shot id arrives from a command line and is
    # interpolated into the env flag, so nothing here is parsed by a shell.
    completed = subprocess.run([executable, *command[1:]], check=False)
    if completed.returncode == 0:
        print(f"\n  Injected. Watch {args.shot} appear on the board, then press Diagnose.\n")
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main())
