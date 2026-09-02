"""The chaos suite builds a command that induces a real fault.

Every scenario here is inducible with the render worker exactly as it is, through
environment the scene already reads. That is the property worth pinning: a chaos suite
whose faults are simulated proves less than three real ones, and the way this stops being
true is someone adding a scenario with a flag nothing reads.
"""

import pytest
from dailies_chaos.cli import build_command
from dailies_chaos.scenarios import SCENARIOS, find

# Every environment variable a scenario is allowed to set. Each is read by the render
# worker or by scenes/demo_scene.py today; a scenario setting anything else would produce
# a render that ignores it and a demo that proves nothing.
READ_BY_THE_WORKER = {
    "DAILIES_MISSING_TEXTURE",
    "DAILIES_MISSING_TEXTURE_PATH",
    "DAILIES_SAMPLES",
    "DAILIES_RESOLUTION_X",
    "DAILIES_RESOLUTION_Y",
}


def env_of(command: list[str]) -> dict[str, str]:
    flag = next(arg for arg in command if arg.startswith("--update-env-vars="))
    return dict(pair.split("=", 1) for pair in flag.split("=", 1)[1].split(","))


class TestTheScenariosAreReal:
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
    def test_every_scenario_sets_only_variables_something_reads(self, scenario):
        unread = set(scenario.env) - READ_BY_THE_WORKER
        assert not unread, f"{scenario.name} sets {unread}, which nothing reads"

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
    def test_every_scenario_says_what_it_proves_and_expects(self, scenario):
        """A fault nobody can grade is a light show. Each one states what the agent should
        conclude, so a demo run has a pass and a fail rather than a shrug."""
        assert len(scenario.proves) > 60, scenario.name
        assert len(scenario.expect) > 30, scenario.name

    def test_the_three_faults_are_distinct_in_kind(self):
        """Silent bad output, delivery risk with no failure, and a loud crash. Three of
        the same kind would be one scenario with three spellings."""
        assert {s.name for s in SCENARIOS} == {"missing-texture", "slow-frame", "worker-oom"}


class TestTheCommand:
    def test_it_targets_the_render_job_with_the_fault_applied(self):
        command = build_command(find("missing-texture"), shot="SH400")
        assert command[:5] == ["gcloud", "run", "jobs", "execute", "dailies-render"]
        assert env_of(command)["DAILIES_MISSING_TEXTURE"] == "1"
        assert env_of(command)["DAILIES_SHOT"] == "SH400"

    def test_an_induced_shot_is_labelled_as_induced(self):
        """A chaos render must never be mistaken later for something the farm did by
        itself. The render_job label carries that into the telemetry, where it survives."""
        assert env_of(build_command(find("worker-oom"), shot="SH401"))["DAILIES_RENDER_JOB"] == (
            "chaos-worker-oom"
        )

    def test_the_scenario_env_wins_over_the_defaults(self):
        command = build_command(find("worker-oom"), shot="SH402")
        assert env_of(command)["DAILIES_RESOLUTION_X"] == "3840"

    def test_frame_count_comes_from_the_scenario(self):
        """A 4K frame is rendered once. Four of them is a demo nobody waits through."""
        assert env_of(build_command(find("worker-oom"), shot="SH403"))["DAILIES_FRAME_END"] == "1"
        assert (
            env_of(build_command(find("missing-texture"), shot="SH404"))["DAILIES_FRAME_END"] == "4"
        )

    def test_no_shell_metacharacters_reach_a_shell(self):
        """argv, never a string. The shot id comes from a command line and is interpolated
        into the env flag, so this stays a list that subprocess passes without a shell."""
        command = build_command(find("slow-frame"), shot="SH405")
        assert isinstance(command, list)
        assert all(isinstance(part, str) for part in command)


class TestLookup:
    def test_an_alias_resolves(self):
        assert find("oom") is find("worker-oom")

    def test_lookup_is_case_and_space_insensitive(self):
        assert find("  Missing-Texture ") is find("missing-texture")

    def test_an_unknown_name_is_none_rather_than_a_guess(self):
        assert find("delete-everything") is None


class TestRunningIt:
    def test_it_execs_the_resolved_path_not_the_bare_name(self, monkeypatch):
        """On Windows gcloud is a .cmd and subprocess does not apply PATHEXT, so
        ["gcloud", ...] raises FileNotFoundError on a machine where gcloud is installed
        and on PATH. Driving the tool for real is how this was found: the existence check
        passed and the exec failed one line later."""
        import subprocess as sp

        from dailies_chaos import cli

        seen: dict[str, list[str]] = {}

        def fake_run(argv, **_kwargs):
            seen["argv"] = argv
            return sp.CompletedProcess(argv, 0)

        monkeypatch.setattr(cli.shutil, "which", lambda _name: "C:/sdk/bin/gcloud.cmd")
        monkeypatch.setattr(cli.subprocess, "run", fake_run)
        assert cli.main(["inject", "missing-texture", "--shot", "SH900"]) == 0
        assert seen["argv"][0] == "C:/sdk/bin/gcloud.cmd"
        assert seen["argv"][1:4] == ["run", "jobs", "execute"]

    def test_a_missing_gcloud_is_named_rather_than_crashing(self, monkeypatch, capsys):
        from dailies_chaos import cli

        monkeypatch.setattr(cli.shutil, "which", lambda _name: None)
        assert cli.main(["inject", "missing-texture", "--shot", "SH901"]) == 2
        assert "not on PATH" in capsys.readouterr().err
