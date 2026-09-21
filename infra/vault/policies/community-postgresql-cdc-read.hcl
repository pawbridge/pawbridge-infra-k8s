# Bind only community-postgresql-cdc-vault-auth in namespace kafka, audience vault.
path "secret/data/pawbridge/dev/community/postgresql-cdc" {
  capabilities = ["read"]
}
