# Where the three images live. Terraform owns the repository; it does not own its
# contents.

resource "google_artifact_registry_repository" "dailies" {
  location      = var.region
  repository_id = "dailies"
  description   = "Container images for Dailies (api, web, render)"
  format        = "DOCKER"
}
