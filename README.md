# Dailies

**Can tonight's renders make tomorrow morning's dailies? Dailies knows before the farm does.**

Dailies is an AI reliability layer for render pipelines. It observes live rendering
infrastructure through Grafana, predicts delivery risk, investigates failures, inspects
rendered output for visual failures, and coordinates recovery before production deadlines
are missed.

> A render pipeline is a distributed production system whose SLO is not uptime.
> Its SLO is delivering the right frames before a creative deadline.

## What it is not

Not a render manager. Deadline Cloud, OpenCue and Flamenco already do that well. Dailies
sits above whatever farm is underneath; Cloud Run + Blender is the first backend adapter,
not the product.

## Status

Early development. Built for the [Agentic Cinema](https://agentic-cinema.devpost.com)
hackathon, Grafana track, and designed to outlive it.

## Layout

```
apps/api/            FastAPI service: agent host + board API
apps/web/            Next.js delivery board
packages/telemetry/  render telemetry schema, Blender parser, OTLP emitter
packages/render/     RenderBackend protocol + Blender/Cloud Run adapters
chaos/               deterministic failure injection
dashboards/          Grafana dashboards
evals/               evaluation harness
```

## Licence

Apache-2.0.
