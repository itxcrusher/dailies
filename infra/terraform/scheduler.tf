# Keeping the board alive.
#
# The board is built from telemetry with a 24-hour lookback, and it holds no state of its
# own: no seeding step, no fixtures, nothing to fall back on. That is the right design and
# it has one consequence nobody had thought through. **A farm that stops rendering has an
# empty board within a day.**
#
# Measured on 2026-09-01, the morning after the last manual render: the hosted URL read
# "0 WATCHED - No shots are being watched yet". Nothing had failed. The deploy was green,
# every test passed, and the product showed nothing. It is precisely the failure this
# project exists to catch, happening to this project.
#
# That matters because judging runs 2026-09-10 to 2026-10-08 and the rules require a URL a
# judge can open. Without these, every judge in that four-week window opens an empty page.
#
# So the farm renders on a schedule. Cheap, and it is also the honest demo: the board is
# not a mock, and the way to prove that is to let it keep filling itself.

locals {
  # The shots the scheduled farm renders, and the story the board tells at a glance.
  #
  # Two clean and one broken, deliberately in that ratio. The entire thesis is that the
  # broken one is INDISTINGUISHABLE from the others by every number a scheduler looks at,
  # and a board where half the rows are failures does not show that; it shows a farm on
  # fire. The exception has to look like an exception.
  #
  # Minutes are staggered so three Blender jobs do not start at the same instant and
  # contend for the same quota.
  scheduled_shots = {
    "vqa-good" = {
      shot    = "SH200"
      minute  = 0
      broken  = false
      comment = "the clean render every other row is compared against"
    }
    "seq-b" = {
      shot    = "SH205"
      minute  = 12
      broken  = false
      comment = "a second healthy shot, so the board reads as a farm and not a pair"
    }
    "vqa-bad" = {
      shot    = "SH201"
      minute  = 24
      broken  = true
      comment = "exit code 0, every frame present, and the jacket is flat magenta"
    }
  }
}

# Its own identity, not the runtime service account.
#
# The runtime account reaches Vertex, Secret Manager and the frames bucket because the API
# needs all three. A timer needs exactly one verb: start this job. Reusing the runtime
# account would hand a public-internet-triggerable scheduler every permission the agent
# has, for no benefit beyond one fewer resource here.
resource "google_service_account" "scheduler" {
  account_id   = "dailies-scheduler"
  display_name = "Starts the render job on a timer so the board is never empty"
}

# Two permissions, and the second one is the whole reason this role exists.
#
# roles/run.invoker looks right and is what the obvious reading of the docs suggests. It
# carries run.jobs.run, so it starts a job fine. It does NOT carry
# run.jobs.runWithOverrides, and this scheduler sends overrides on every call, because one
# job definition serves three shots. The first apply used run.invoker and every attempt
# came back PERMISSION_DENIED with an empty execution list: the scheduler was enabled, the
# target was correct, and nothing ran.
#
# The role that does carry it is roles/run.developer, which also grants create, delete and
# update on Cloud Run jobs. That is a strange amount of authority for a timer, so this
# grants the two verbs and nothing else.
resource "google_project_iam_custom_role" "job_runner" {
  role_id     = "dailiesJobRunner"
  title       = "Run a Cloud Run job with overrides"
  description = "Start a job and pass per-execution overrides. No create, update or delete."
  permissions = [
    "run.jobs.run",
    "run.jobs.runWithOverrides",
  ]
}

# Bound on the one job, not project-wide.
resource "google_cloud_run_v2_job_iam_member" "scheduler_runs_render" {
  project  = google_cloud_run_v2_job.render.project
  location = google_cloud_run_v2_job.render.location
  name     = google_cloud_run_v2_job.render.name
  role     = google_project_iam_custom_role.job_runner.name
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

resource "google_cloud_scheduler_job" "render" {
  for_each = local.scheduled_shots

  name        = "dailies-render-${each.key}"
  region      = var.region
  description = "${each.value.shot}: ${each.value.comment}"

  # Every six hours, so four renders of each shot sit inside the board's 24-hour lookback
  # at any moment. One a day would leave the board empty for the hours around the gap,
  # which is the failure this is here to prevent rather than a smaller version of it.
  schedule  = "${each.value.minute} */6 * * *"
  time_zone = "Etc/UTC"

  # A render is minutes, and the point is to start it, not to wait for it. The execution
  # outlives this call.
  attempt_deadline = "320s"

  retry_config {
    retry_count = 1
  }

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/${google_cloud_run_v2_job.render.id}:run"

    # Overrides, so one job definition serves every shot. Env named here replaces the
    # image's value; everything else in the job's own definition is untouched, which is
    # what keeps the OTLP wiring and the bucket mount out of this file.
    body = base64encode(jsonencode({
      overrides = {
        containerOverrides = [{
          env = concat(
            [
              { name = "DAILIES_SHOT", value = each.value.shot },
              { name = "DAILIES_RENDER_JOB", value = each.key },
            ],
            # Only on the broken shot. The scene reads this and points the cube's material
            # at a texture that is not there; Blender warns, substitutes a flat colour and
            # exits 0, which is the whole demonstration.
            each.value.broken ? [{ name = "DAILIES_MISSING_TEXTURE", value = "1" }] : [],
          )
        }]
      }
    }))

    headers = {
      "Content-Type" = "application/json"
    }

    oauth_token {
      service_account_email = google_service_account.scheduler.email
      scope                 = "https://www.googleapis.com/auth/cloud-platform"
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.scheduler_runs_render]
}
