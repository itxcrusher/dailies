# Provider and backend configuration.
#
# State is deliberately local. A GCS backend would need a bucket, which would need a
# bootstrap apply, which is a chicken-and-egg for zero benefit on a single-environment
# project with one operator. State is gitignored (see the repository .gitignore) because
# it records resource attributes verbatim.

terraform {
  required_version = ">= 1.9"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
