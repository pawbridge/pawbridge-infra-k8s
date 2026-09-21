# Bind only store-postgresql-cdc-vault-auth in namespace kafka, audience vault.
path "secret/data/pawbridge/dev/store/postgresql-cdc" {
  capabilities = ["read"]
}
