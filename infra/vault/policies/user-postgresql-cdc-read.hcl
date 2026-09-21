# Bind only user-postgresql-cdc-vault-auth in namespace kafka, audience vault.
path "secret/data/pawbridge/dev/user/postgresql-cdc" {
  capabilities = ["read"]
}
