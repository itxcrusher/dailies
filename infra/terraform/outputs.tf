# What the next steps need. Task 11 drives these URLs with curl rather than trusting the
# apply; an output is a claim about what was created, not evidence that it serves.

output "api_url" {
  description = "Public URL of the FastAPI service."
  value       = google_cloud_run_v2_service.api.uri
}

output "web_url" {
  description = "Public URL of the board."
  value       = google_cloud_run_v2_service.web.uri
}

output "render_job_name" {
  description = "Cloud Run job to execute for a render."
  value       = google_cloud_run_v2_job.render.name
}

output "runtime_service_account" {
  description = "Identity every container runs as."
  value       = google_service_account.runtime.email
}

output "artifact_registry_repository" {
  description = "Docker repository path the three images are pushed to."
  value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.dailies.repository_id}"
}
