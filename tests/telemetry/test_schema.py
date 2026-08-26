from dailies_telemetry.schema import LABELS, METRICS, EventKind, RenderEvent


def test_metric_names_are_prometheus_safe():
    for name in METRICS.values():
        assert name.startswith("render_")
        assert name.islower()
        assert " " not in name


def test_render_event_requires_shot_and_frame():
    e = RenderEvent(
        kind=EventKind.FRAME_COMPLETE,
        shot="SH010",
        frame=1,
        duration_seconds=12.5,
        memory_bytes=1024,
    )
    assert e.shot == "SH010"
    assert e.labels()["shot"] == "SH010"
    assert set(e.labels()) == set(LABELS)
