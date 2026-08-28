"""The scene the render container falls back to when no .blend is mounted.

Run by Blender, not by pytest: it imports ``bpy``, which only exists inside Blender's own
interpreter. Blender executes it with ``--python`` after the factory startup file is
loaded, so it edits the default scene (a cube, a camera and a light) rather than building
one from nothing.

Why a script and not a checked-in .blend: a .blend is an opaque binary that nobody can
review in a diff, and the one thing this scene has to be is *tunable*. Task 12 induces a
real out-of-memory failure by making a frame heavy enough to exceed the job's memory
limit, and that is a two-variable change here (``DAILIES_SAMPLES``, ``DAILIES_RESOLUTION``)
rather than a re-export of an asset.

Cycles rather than EEVEE, and CPU rather than GPU: Cloud Run has no GPU, EEVEE needs a
GL context that a background container does not have, and Cycles is what a render farm
actually runs. Every knob is read from the environment so the same image can render a
cheap smoke frame and a deliberately heavy one.
"""

import os

import bpy


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # Print rather than raise: a typo in a tuning variable should not cost the render.
        print(f"Warning: {name}={raw!r} is not an integer; using {default}")
        return default


def configure(scene: "bpy.types.Scene") -> None:
    """Point the scene at Cycles on the CPU, at the size and sample count asked for."""
    samples = _int_env("DAILIES_SAMPLES", 64)
    width = _int_env("DAILIES_RESOLUTION_X", 640)
    height = _int_env("DAILIES_RESOLUTION_Y", 360)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = samples
    # Denoising is off on purpose. It allocates a second full-resolution buffer, which
    # changes the memory ceiling this scene renders under, and that ceiling is the thing
    # Task 12 deliberately pushes past. Leaving it on would make the OOM depend on a
    # setting nobody is looking at.
    scene.cycles.use_denoising = False
    # Adaptive sampling stops early on easy pixels, so two runs of the "same" frame take
    # different amounts of time. A render-duration histogram wants the sample count to
    # mean something, so it is off here.
    scene.cycles.use_adaptive_sampling = False

    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    # Every core: the job asks Cloud Run for 4 CPUs and a render that uses one of them
    # would make the duration series a measure of the container, not the frame.
    scene.render.threads_mode = "AUTO"

    # No frame range in this line, deliberately. Blender runs --python BEFORE it applies
    # --frame-start/--frame-end, so scene.frame_start here is still the startup file's
    # 1-250 and printing it announced a range the render was never going to use. In a
    # project whose whole premise is diagnosing renders from their logs, a log line that
    # confidently states the wrong range is worse than no line at all.
    print(f"dailies: cycles CPU, {width}x{height}, {samples} samples")


def animate(scene: "bpy.types.Scene") -> None:
    """Give the cube a rotation so consecutive frames are not identical images.

    Identical frames would render at identical cost, which makes the duration histogram
    a flat line and the whole "is this shot on pace" question meaningless to demonstrate.
    """
    cube = bpy.data.objects.get("Cube")
    if cube is None:  # pragma: no cover - only if the startup file changes upstream
        print("Warning: no Cube in the factory startup scene; rendering it unanimated")
        return
    # Keyframes at the ends of a nominal 24-frame turn. The render's own frame range is
    # set on the command line and may be a subset of it; the interpolation still holds.
    cube.rotation_euler = (0.0, 0.0, 0.0)
    cube.keyframe_insert(data_path="rotation_euler", frame=1)
    cube.rotation_euler = (0.0, 0.0, 6.283185)
    cube.keyframe_insert(data_path="rotation_euler", frame=24)


configure(bpy.context.scene)
animate(bpy.context.scene)
