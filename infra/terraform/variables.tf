# Inputs.
#
# The three image variables are the seam between Terraform and the build. Terraform owns
# durable infrastructure; images change on every deploy and are built by `gcloud builds
# submit` (see Task 10 of the plan). Holding an image digest in state would turn a code
# change into a full plan/apply cycle and make the two tools fight over the same field.

variable "project_id" {
  type        = string
  description = "GCP project that owns every resource here (dailies-render-2026)."
}

variable "region" {
  type        = string
  description = "Region for Artifact Registry, both Cloud Run services and the render job."
  default     = "us-central1"
}

variable "api_image" {
  type        = string
  description = <<-EOT
    Fully qualified image for the FastAPI service, e.g.
    us-central1-docker.pkg.dev/dailies-render-2026/dailies/api:v1.
    Built and pushed outside Terraform by `gcloud builds submit`; Terraform only
    references the tag it is given.
  EOT
}

variable "web_image" {
  type        = string
  description = <<-EOT
    Fully qualified image for the Next.js board. Built and pushed outside Terraform,
    exactly as api_image is.
  EOT
}

variable "render_image" {
  type        = string
  description = <<-EOT
    Fully qualified image for the Blender render worker. Built and pushed outside
    Terraform, exactly as api_image is.
  EOT
}

variable "grafana_url" {
  type        = string
  description = "Base URL of the Grafana Cloud stack, e.g. https://politebamboo549.grafana.net."
}

variable "otlp_endpoint" {
  type        = string
  description = "Grafana Cloud OTLP gateway the render worker pushes metrics to."
  default     = "https://otlp-gateway-prod-us-east-3.grafana.net/otlp"
}

variable "prometheus_datasource_uid" {
  type        = string
  description = "Grafana datasource uid the agent queries for metrics."
  default     = "grafanacloud-prom"
}

variable "loki_datasource_uid" {
  type        = string
  description = "Grafana datasource uid the agent queries for logs."
  default     = "grafanacloud-logs"
}

variable "vertex_location" {
  type        = string
  description = "Vertex AI location the ADK agent calls Gemini in."
  default     = "us-central1"
}

variable "cors_origins" {
  type        = string
  description = <<-EOT
    Comma-separated browser origins allowed to read the API (DAILIES_CORS_ORIGINS).
    Left empty by default on purpose: the board's URL is only known after its service
    exists, and wiring web.uri into the API here would make the two services depend on
    each other in both directions. Set it in terraform.tfvars once the board URL is known,
    or leave it empty if the board reads the API server-side.
  EOT
  default     = ""
}

variable "render_job_timeout" {
  type        = string
  description = "Wall-clock limit for one render execution."
  default     = "3600s"
}

variable "render_job_cpu" {
  type        = string
  description = "CPU for the render job. Blender is the only thing in this system that needs real compute."
  default     = "4"
}

variable "render_job_memory" {
  type        = string
  description = <<-EOT
    Memory for the render job. Deliberately a variable: Task 12 induces a real OOM by
    lowering it, and that has to be a one-line change rather than an edit to a resource.

    Cloud Run enforces a memory floor per CPU: at render_job_cpu = "4" the minimum is
    "2Gi". Squeezing below that means lowering render_job_cpu in the same change, or the
    revision is rejected for the wrong reason and no OOM is ever observed.
  EOT
  default     = "8Gi"
}
