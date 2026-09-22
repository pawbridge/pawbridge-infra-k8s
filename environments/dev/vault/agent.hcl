# Fetch local-dev secrets once, render both files, then exit. No application YAML generation.
exit_after_auth = true
pid_file = "/tmp/vault-agent.pid"
log_level = "warn"

vault {
  address = "https://192.168.57.12:30820"
  ca_cert = "/tls/ca.crt"
  tls_server_name = "vault.vault.svc.cluster.local"
  retry {
    num_retries = 2
  }
}
auto_auth {
  method "approle" {
    mount_path = "auth/pawbridge-local-dev"
    exit_on_err = true
    config = {
      role_id_file_path = "/auth/role-id"
      secret_id_file_path = "/auth/secret-id"
      remove_secret_id_file_after_reading = false
    }
  }
}
template_config {
  exit_on_retry_failure = true
}
template {
  source = "/config/runtime.ctmpl"
  destination = "/output/runtime.env"
  perms = "0600"
  error_on_missing_key = true
}
template {
  source = "/config/google.ctmpl"
  destination = "/output/google.env"
  perms = "0600"
  error_on_missing_key = true
}
