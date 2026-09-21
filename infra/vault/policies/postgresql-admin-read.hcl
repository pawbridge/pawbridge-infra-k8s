# Proposed Kubernetes auth role: postgresql-admin-read
# Bind only service account postgresql-admin-vault-auth in namespace databases,
# audience vault. No wildcard, list or write capability.
path "secret/data/pawbridge/dev/postgresql/admin" {
  capabilities = ["read"]
}
