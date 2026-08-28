# The runtime identity.
#
# A dedicated service account rather than the default compute one: the default is shared
# by everything in the project and carries roles/editor, so a bug in the render worker
# would run with permission to rewrite the project. This account gets exactly two things:
# Vertex AI (the agent calls Gemini) and accessor on the two named secrets, granted
# per-secret in secrets.tf rather than project-wide.

resource "google_service_account" "runtime" {
  account_id   = "dailies-runtime"
  display_name = "Dailies runtime (Cloud Run services and jobs)"
  description  = "Identity for the API, the board and the render job."
}

resource "google_project_iam_member" "runtime_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}
