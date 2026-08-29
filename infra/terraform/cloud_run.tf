# The three runtime surfaces: the API, the board, and the render worker.
#
# Services for the two things a judge opens in a browser, a Job for the render, because a
# render is a bounded task that must be able to FAIL and stay failed. Every container runs
# as the dedicated runtime service account and receives its Grafana credentials by
# reference; nothing here interpolates a secret value.

locals {
  # The Python OTLP exporter parses OTEL_EXPORTER_OTLP_HEADERS as a comma-separated
  # key=value list, so the space in "Basic <base64>" MUST be percent-encoded. A literal
  # space is not rejected loudly: the value is cut at the space and Grafana answers 401
  # with nothing pointing at the cause. The prefix is written once, here, so the encoding
  # lives in exactly one reviewable place instead of being retyped in three Dockerfiles.
  #
  # The container assembles the finished header at startup:
  #
  #   OTEL_EXPORTER_OTLP_HEADERS="$DAILIES_OTLP_HEADER_PREFIX$(printf %s "$GRAFANA_OTLP_AUTH" | base64 -w0)"
  #
  # It is not assembled here. grafana-otlp-auth stores the raw "<instance_id>:<token>"
  # pair, and base64-ing that in HCL means reading the secret VALUE into Terraform, which
  # writes the credential in plaintext into terraform.tfstate AND into the Cloud Run
  # revision spec, readable by anyone with run.services.get. Injecting the pair by
  # reference and encoding it in the entrypoint keeps the credential in Secret Manager.
  otlp_env = {
    OTEL_EXPORTER_OTLP_ENDPOINT = var.otlp_endpoint
    OTEL_EXPORTER_OTLP_PROTOCOL = "http/protobuf"
    DAILIES_OTLP_HEADER_PREFIX  = "Authorization=Basic%20"
  }

  # Everything the agent needs to reach the Grafana stack, minus the token.
  grafana_env = {
    GRAFANA_URL            = var.grafana_url
    GRAFANA_PROMETHEUS_UID = var.prometheus_datasource_uid
    GRAFANA_LOKI_UID       = var.loki_datasource_uid
  }

  # Vertex AI rather than the Gemini Developer API: the ADK routes through Vertex when
  # this is set, which is what the runtime service account is authorised for and what the
  # competition's Google-Cloud-AI-only rule requires.
  vertex_env = {
    GOOGLE_GENAI_USE_VERTEXAI = "TRUE"
    GOOGLE_CLOUD_PROJECT      = var.project_id
    GOOGLE_CLOUD_LOCATION     = var.vertex_location
  }
}

# ---------------------------------------------------------------------------
# API: FastAPI plus the ADK investigator
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "api" {
  name     = "dailies-api"
  location = var.region

  # Judges reach this without a Google account; the authentication half of that is the
  # allUsers invoker binding below.
  ingress = "INGRESS_TRAFFIC_ALL"

  # Off so `terraform destroy` actually tears the environment down. The provider defaults
  # this on, which on a demo environment is a trap rather than a safety net.
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    # An agent investigation is several sequential Grafana queries plus a Gemini call.
    # The platform default would cut the request off mid-answer.
    timeout = "600s"

    containers {
      image = var.api_image

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        # CPU stays allocated for the whole request so the ADK's work between awaits is
        # not throttled to a crawl.
        cpu_idle          = false
        startup_cpu_boost = true
      }

      # Where the investigator reaches Grafana. The URL is taken from the MCP service's
      # own attribute rather than typed out: Cloud Run mints the hostname, so a literal
      # here would be a guess that survives every plan and fails at the first
      # investigation. Referencing it also makes Terraform order the two services.
      #
      # The two datasource UIDs come from the same variables as GRAFANA_PROMETHEUS_UID /
      # GRAFANA_LOKI_UID above (defaulting to grafanacloud-prom and grafanacloud-logs)
      # rather than being retyped as literals here. Two spellings of one UID in one
      # container is a divergence waiting to happen, and the half that is wrong would not
      # fail: a valid-but-wrong UID answers about a different datasource.
      dynamic "env" {
        for_each = merge(local.grafana_env, local.vertex_env, local.otlp_env, {
          DAILIES_CORS_ORIGINS   = var.cors_origins
          DAILIES_MCP_URL        = google_cloud_run_v2_service.mcp_grafana.uri
          DAILIES_PROMETHEUS_UID = var.prometheus_datasource_uid
          DAILIES_LOKI_UID       = var.loki_datasource_uid
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      env {
        name = "GRAFANA_SERVICE_ACCOUNT_TOKEN"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.grafana_token.secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "GRAFANA_OTLP_AUTH"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.grafana_otlp.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  # The secrets are referenced by name, so no resource attribute makes Terraform order
  # these. Cloud Run checks the accessor grant when it creates the revision, and without
  # this the first apply fails on a race that a second apply would pass.
  depends_on = [
    google_secret_manager_secret_iam_member.runtime_grafana_token,
    google_secret_manager_secret_iam_member.runtime_grafana_otlp,
  ]
}

# ---------------------------------------------------------------------------
# Board: the Next.js surface a judge actually looks at
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "web" {
  name                = "dailies-web"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 3
    }

    containers {
      image = var.web_image

      ports {
        container_port = 3000
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        startup_cpu_boost = true
      }

      # Read this server-side. A NEXT_PUBLIC_* variable is inlined at BUILD time, so
      # setting one here would reach the container and never reach the browser bundle:
      # a board that renders with no data and no error.
      env {
        name  = "DAILIES_API_URL"
        value = google_cloud_run_v2_service.api.uri
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Public access
#
# Both surfaces must answer an unauthenticated request: a judge opens a link, and a login
# wall reads as a broken submission. The render job stays private; nothing outside the
# project should be able to start a render.
#
# PREREQUISITE, and it will fail the apply otherwise. This project inherits
# constraints/iam.allowedPolicyMemberDomains from the infraforge.agency organisation,
# restricted to customer id C01saw3ve (verified 2026-08-28 with
# `gcloud resource-manager org-policies describe ... --effective`). That constraint
# rejects "allUsers", so these two bindings fail with "One or more users named in the
# policy do not belong to a permitted customer" until an exception is set for
# dailies-render-2026. Setting it is an organisation-policy-admin operation, which is one
# of the few things that needs admin@infraforge.agency rather than the gmail account.
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service_iam_member" "api_public" {
  project  = google_cloud_run_v2_service.api.project
  location = google_cloud_run_v2_service.api.location
  name     = google_cloud_run_v2_service.api.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "web_public" {
  project  = google_cloud_run_v2_service.web.project
  location = google_cloud_run_v2_service.web.location
  name     = google_cloud_run_v2_service.web.name
  role     = "roles/run.invoker"
  member   = "allUsers"
}

# ---------------------------------------------------------------------------
# Render worker
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "render" {
  name                = "dailies-render"
  location            = var.region
  deletion_protection = false

  template {
    # One shot per execution. Parallelism belongs to whatever submits the executions, not
    # to the job definition.
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runtime.email

      # ZERO, and load-bearing rather than a default left alone. Task 12 induces a real
      # OOM by starving this job of memory, and the point is that the failure stays failed
      # and observable. A retry would quietly succeed on a lighter frame and erase the
      # incident the investigator is supposed to diagnose.
      max_retries = 0

      timeout = var.render_job_timeout

      containers {
        image = var.render_image

        resources {
          limits = {
            cpu    = var.render_job_cpu
            memory = var.render_job_memory
          }
        }

        dynamic "env" {
          for_each = local.otlp_env
          content {
            name  = env.key
            value = env.value
          }
        }

        env {
          name = "GRAFANA_OTLP_AUTH"
          value_source {
            secret_key_ref {
              secret  = data.google_secret_manager_secret.grafana_otlp.secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.runtime_grafana_otlp,
  ]
}

# The Grafana MCP server.
#
# The track rule is explicit that "the MCP server connection is what's checked", so this
# is the load-bearing integration rather than a convenience: the investigator reaches
# Prometheus and Loki through this process, not through the Grafana HTTP API directly.
#
# Deliberately NOT public. Unlike the board and its API, nothing outside this project has
# any reason to reach it, and it holds a Grafana service-account token. Only
# google_service_account.runtime can invoke it.
resource "google_cloud_run_v2_service" "mcp_grafana" {
  name                = "dailies-mcp-grafana"
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account = google_service_account.runtime.email

    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }

    containers {
      image = var.mcp_grafana_image

      # mcp-grafana validates the Host header on EVERY route and answers an unknown host
      # with "forbidden: host not allowed". Its default allowlist is loopback only, so a
      # Cloud Run hostname has to be named here or nothing can reach it. The upstream
      # README notes "*" is only safe behind a trusted reverse proxy; naming the host
      # costs nothing and keeps the DNS-rebinding protection doing its job.
      args = [
        "-t", "streamable-http",
        "--allowed-hosts", var.mcp_grafana_host,
      ]

      ports {
        container_port = 8000
      }

      env {
        name  = "GRAFANA_URL"
        value = var.grafana_url
      }

      env {
        name = "GRAFANA_API_KEY"
        value_source {
          secret_key_ref {
            secret  = data.google_secret_manager_secret.grafana_token.secret_id
            version = "latest"
          }
        }
      }

      resources {
        limits = {
          cpu    = "1"
          memory = "512Mi"
        }
        startup_cpu_boost = true
      }
    }
  }
}

# Only the runtime service account may invoke it. No allUsers binding here on purpose:
# this service is not part of what a judge needs to reach, and it fronts a credential.
resource "google_cloud_run_v2_service_iam_member" "mcp_grafana_runtime" {
  location = google_cloud_run_v2_service.mcp_grafana.location
  name     = google_cloud_run_v2_service.mcp_grafana.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.runtime.email}"
}
