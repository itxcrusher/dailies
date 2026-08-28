# The two Grafana credentials.
#
# Both secrets ALREADY EXIST and already hold real values, created out of band so that no
# credential ever passed through a file in this repository. They are read here through
# `data` sources on purpose. A `resource "google_secret_manager_secret"` block would claim
# ownership of them: Terraform would see resources it did not create, and a later
# destroy or a rename would delete the stored credentials. `data` can only read.
#
#   grafana-token      the Grafana service-account token the MCP client authenticates with
#   grafana-otlp-auth  "<instance_id>:<otlp_token>" for the Grafana Cloud OTLP gateway
#
# Neither value is read into Terraform. There is a data source
# (google_secret_manager_secret_version) that would return the plaintext, and it is
# avoided deliberately: its result is written verbatim into terraform.tfstate and, if
# interpolated into an env var, into the Cloud Run revision spec, where anyone with
# run.services.get can read it. The values reach the containers by reference only, through
# value_source.secret_key_ref in cloud_run.tf.

data "google_secret_manager_secret" "grafana_token" {
  secret_id = "grafana-token"
}

data "google_secret_manager_secret" "grafana_otlp" {
  secret_id = "grafana-otlp-auth"
}

# Accessor is granted on each secret, not on the project. roles/secretmanager.secretAccessor
# at project level would hand the runtime every secret the project ever gains, including
# ones added later by someone who never looked at this file.

resource "google_secret_manager_secret_iam_member" "runtime_grafana_token" {
  secret_id = data.google_secret_manager_secret.grafana_token.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_secret_manager_secret_iam_member" "runtime_grafana_otlp" {
  secret_id = data.google_secret_manager_secret.grafana_otlp.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}
