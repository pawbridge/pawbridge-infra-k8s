# Bind only animal-postgresql-cdc-vault-auth in namespace kafka, audience vault.
path "secret/data/pawbridge/dev/animal/postgresql-cdc" {
  capabilities = ["read"]
}
