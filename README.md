# Dailies

**A render farm knows how to handle a crash. It has no idea what to do with a frame that worked.**

A texture fails to resolve on the worker. The renderer substitutes a placeholder, prints a warning, and **exits 0**. The frame saves. The frame count is complete. The durations look normal. Every automated system in the pipeline calls it a success, and the jacket comes back flat magenta.

Nobody finds out until a human watches the dailies.

Dailies is an AI reliability layer that catches that from telemetry, before anyone watches anything. A Gemini agent reads live render telemetry **through the Grafana MCP server**, rates delivery risk against the creative deadline, diagnoses failures with a citation for every claim, and then opens the frame the render actually produced and says whether the picture is wrong.

**Live:** <https://dailies-web-3tc7ky4kha-uc.a.run.app>

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
python -m pytest -q                 # 557 tests
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
scenes/              the Blender scene the demo renders, including the broken variant
infra/               Dockerfiles, Cloud Build config, Terraform
tests/               557 tests, heaviest over the parser and the agent contracts
```

## Notes on the engineering

The parser is where render-domain knowledge lives. It is pure, and it carries the heaviest coverage in the repo, because everything downstream is a consequence of reading Blender's stdout correctly.

**Third-party surfaces are verified against the live thing before code depends on them**, and that rule was written after being taught. `gemini-3.7-flash` is real on the Gemini API and returns 404 on Vertex; every test injected a fake model, so the whole suite passed while the deployed agent could not answer at all. The same shape recurred five more times: a Cloud Run hostname the MCP server refused, a Loki stream selector on a label that is only structured metadata, a PromQL step wider than the staleness window, an OTel unit suffix appended to a metric name, and a zero-byte directory placeholder that Gemini rejected as an invalid image.

Every one produced **an empty result rather than an error**. That is the lesson worth carrying out of this repo, and it is why the agent is told, in its instructions, that an empty result on this stack is far more often a defect in the query than an absence in the data.

## Licence

Apache-2.0.
