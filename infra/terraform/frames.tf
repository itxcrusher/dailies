# Where rendered frames live, so something can look at them.
#
# Until now a frame was written to /tmp inside the render container and died with it.
# That was fine while the project reasoned about renders from telemetry alone, and it is
# the blocker for Visual QA: "the jacket is grey" is currently inferred from a log line
# saying an asset could not be opened, which is evidence about the *cause*. Looking at
# the picture is evidence about the *deliverable*, and it is the only way to catch the
# failures that leave no log at all - a wrong camera, broken geometry, a black frame.

resource "google_storage_bucket" "frames" {
  name     = "${var.project_id}-frames"
  location = var.region

  # Uniform access, so a frame's readability is decided by IAM on the bucket rather than
  # by per-object ACLs that drift. Nothing here is public: the API reads frames with the
  # runtime service account and hands the bytes to Gemini itself.
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Frames are large, numerous and regenerable, and this is a hackathon project on a
  # trial billing account. Thirty days is longer than any demo needs and short enough
  # that a forgotten render loop cannot quietly accumulate a bill.
  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  # A render is repeatable and a frame is not worth versioning: an overwrite means the
  # shot was re-rendered, which is the newer truth.
  versioning {
    enabled = false
  }

  force_destroy = true
}

# The render job writes frames; the API reads them to show Gemini. One service account
# does both, so this is a single binding rather than two roles split by direction.
resource "google_storage_bucket_iam_member" "frames_runtime" {
  bucket = google_storage_bucket.frames.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.runtime.email}"
}

output "frames_bucket" {
  description = "Bucket the render job writes frames into and Visual QA reads them from."
  value       = google_storage_bucket.frames.name
}
