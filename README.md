# Dailies

**A render farm knows how to handle a crash. It has no idea what to do with a frame that worked.**

A texture fails to resolve on the worker. The renderer substitutes a placeholder, prints a warning, and **exits 0**. The frame saves. The frame count is complete. The durations look normal. Every automated system in the pipeline calls it a success, and the jacket comes back flat magenta.

Nobody finds out until a human watches the dailies.

Dailies is an AI reliability layer that catches that from telemetry, before anyone watches anything. A Gemini agent reads live render telemetry **through the Grafana MCP server**, rates delivery risk against the creative deadline, diagnoses failures with a citation for every claim, and then opens the frame the render actually produced and says whether the picture is wrong.

**Live:** <https://dailies-web-3tc7ky4kha-uc.a.run.app>

**Dashboard:** <https://politebamboo549.grafana.net/public-dashboards/2f36b669818743dcbe8d195b79175af9> - the render pipeline and, underneath it, the agent that reads the render pipeline. No login.

## The failure this exists for

This is not a hypothetical. It is how render farms describe their own worst day:

> "The material will fail to load silently, rendering as black. The render completes without error messages, but the output is incorrect."
>
> "Even if only one texture is missing, the render will submit a 'done' status. You discover it after the render completes, and you have already paid for those broken frames." - [iRender](https://irendering.net/missing-textures-on-your-render-farm-output-heres-the-fix-step-by-step/)

Studios already guard against this with pre-flight path checkers, and well. Dailies is not a replacement for one and does not claim to be: a pre-flight check validates what is true at submission, and cannot see the worker. A mount that drops, a permission change, a path valid on one node and not another, an asset overwritten after submit - those resolve at submit time and fail at render time, and the only place they appear is in telemetry and in the picture.

## What it does, demonstrated

Two shots rendered from the same scene, one with a texture deliberately missing. Same agent, same tools:

| | Telemetry says | The frame says |
| --- | --- | --- |
| SH200 | completed 1 of 1, no errors | `looks_correct` - "a simple grey cube" |
| SH201 | "completed all expected frames, **but the output is defective**: `/assets/jacket_diffuse.exr`" | `suspect` - "flat, saturated purple" |

SH201 is the whole argument. **Every number a scheduler looks at says success.** The exit code is 0, the frame count is complete, the durations are normal. The evidence is a log line and the picture itself.

**The two checks are independent, and that is the design.** The visual check is told how renderers signal a failed texture and nothing at all about what this shot's metrics or logs reported, so it can disagree with them. Two sources that cannot disagree are one source wearing two hats. The board says which case it is.

**Every claim carries its query.** A model asked for a cause will produce one whether or not it looked, so the diagnosis schema refuses an answer with no evidence, and the board renders the queries verbatim. A reviewer can paste one into Grafana and check it in ten seconds. The visual verdict names the frame it judged, for the same reason.

## Running it

Requires Python 3.11+, Node 20+, a Grafana Cloud stack and a Google Cloud project with Vertex AI enabled.

```bash
# 1. Install
pip install -e ".[dev,agent]"
(cd apps/web && npm install)

# 2. Point at Grafana. Copy .env.example to .env and fill it in:
#    GRAFANA_URL, GRAFANA_SERVICE_ACCOUNT_TOKEN, and the OTLP endpoint + credential.
cp .env.example .env

# 3. Run the gate
python -m pytest -q                 # 580 tests
python -m ruff check . && python -m ruff format --check .
(cd apps/web && npm test && npx tsc --noEmit)

# 4. Run a render locally. No Grafana needed; it renders and prints its own telemetry.
docker build -f infra/Dockerfile.render -t dailies-render .
docker run --rm -e DAILIES_SHOT=SH010 -e DAILIES_FRAME_END=2 dailies-render

# 5. Serve the board against an API
(cd apps/web && DAILIES_API_URL=http://127.0.0.1:8080 npm run dev)
```

Deploying the whole thing is Terraform:

```bash
cd infra/terraform
cp terraform.tfvars.example terraform.tfvars   # project, region, image tags
terraform init && terraform apply
```

That provisions Cloud Run services for the API, the board and a private Grafana MCP server, a Cloud Run job for the renderer, Artifact Registry, Secret Manager references and the frames bucket. **Two applies are needed on a clean project**, because the MCP server validates its own `Host` header and its hostname is not known until the service exists. `infra/terraform/variables.tf` says so where it matters.

## Layout

```text
apps/api/            FastAPI service: the board API, the investigator, Visual QA
apps/web/            Next.js: the case at /, the board at /board
packages/telemetry/  render telemetry schema, Blender output parser, OTLP emitter
packages/render/     RenderBackend protocol, Blender worker, Cloud Run adapter
packages/graph/      production graph, completion forecast, delivery slack
dashboards/          the Grafana dashboard, and a publisher that refuses empty panels
scenes/              the Blender scene the demo renders, including the broken variant
infra/               Dockerfiles, Cloud Build config, Terraform
tests/               580 tests, heaviest over the parser and the agent contracts
```

## Notes on the engineering

The parser is where render-domain knowledge lives. It is pure, and it carries the heaviest coverage in the repo, because everything downstream is a consequence of reading Blender's stdout correctly.

**Third-party surfaces are verified against the live thing before code depends on them**, and that rule was written after being taught. `gemini-3.7-flash` is real on the Gemini API and returns 404 on Vertex; every test injected a fake model, so the whole suite passed while the deployed agent could not answer at all. The same shape recurred five more times: a Cloud Run hostname the MCP server refused, a Loki stream selector on a label that is only structured metadata, a PromQL step wider than the staleness window, an OTel unit suffix appended to a metric name, and a zero-byte directory placeholder that Gemini rejected as an invalid image.

Every one produced **an empty result rather than an error**. That is the lesson worth carrying out of this repo, and it is why the agent is told, in its instructions, that an empty result on this stack is far more often a defect in the query than an absence in the data.

A second class of defect cost as much and looks nothing like the first. The cooldown that stops the public Diagnose button from being a free Vertex tap read a missing entry as `0.0`, and `0.0` on a monotonic clock is not a moment long past. It is the clock's origin, and the origin is per sandbox. A freshly started Cloud Run instance is seconds old, so every shot restored from storage sat inside a five-minute cooldown and the service refused to diagnose anything for the first five minutes after each cold start, which is the normal path for a visitor arriving at a service that has scaled to zero.

That test could not fail on a developer machine. A workstation has been up for days, so the same code computes an age of several hundred thousand seconds and sails past the cooldown; the bug existed only where the clock was young. It fails now because the clock is an input to the test rather than an ambient fact, which is the general form of the fix: **the environment a test runs in is part of the test, whether or not it is written down.**

## What the agent scores

`python -m dailies_api.evals.harness`, or `gcloud run jobs execute dailies-evals`. Four scenarios, each telemetry with a known answer. Only the network is faked: above the replayed session sit the real MCP wrapper, the real tool routing, the real prompt, a real Gemini call and the real schema validation.

```
scenario            verdict cause evidence honest
missing_texture      pass   pass   pass      -      4 queries
clean_render         pass    -     pass      -      3 queries
stalled_no_errors    pass   pass   pass      -      4 queries
no_telemetry          -     pass   pass     pass    4 queries
4/4
```

One run. A model is sampled, not measured, so that is what happened rather than a rate.

The fixtures are captured from the live stack, not written. An invented fixture encodes what its author believes the farm emits, so an eval built on one scores the agent against that belief. SH200's fixture has **no log lines at all**, because a healthy render is silent and reading silence as a broken query is this repo's recurring failure.

`no_telemetry` is the case the harness exists for, and it found a real defect. Every query returns empty, and the agent's first answer was *"the render for shot SH999 completed successfully without any logged errors"*. It invented no fault, so it passed the fabrication check, and it still asserted an outcome from an empty result. That is this project's own thesis facing the other way, and the more dangerous direction: a supervisor told about a fault that is not there wastes a minute, while one told a broken shot is fine stops looking. The instruction now says to report that the telemetry could not be read and never that the render completed cleanly, and the scenario requires the honest answer rather than merely forbidding the invented one.

## The dashboard

`dashboards/dailies.json` is one dashboard in two halves, which is the point: the render farm on top, and underneath it the Gemini agent that reads the render farm. An agent trusted with a deadline is itself a pipeline, and one nobody can observe is one nobody should trust.

Every panel wraps its metric in `last_over_time(m[$__range])` rather than querying it bare, and that is not a style preference. Prometheus answers an instant query only from a sample inside its five-minute staleness window, and a render that finished an hour ago has none: measured on this stack, `render_job_frames_expected` returns 6 series over a 24h range and **0** as an instant query. Bare metrics would have produced a dashboard that is correct during a render and empty the rest of the time, which is the worst of the two, because it looks right exactly when someone is testing it.

It is public, so it needs no Grafana account to read. `dashboards/publish.py` runs every panel's own query before uploading and refuses to publish one that would render as an empty chart. A dashboard is the one artefact where being wrong is invisible: a query against a metric that does not exist draws the same picture as a quiet pipeline. That guard is tested by pointing a panel at a metric that does not exist and confirming the publish is refused.

## Licence

Apache-2.0.
