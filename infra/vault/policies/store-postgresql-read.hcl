# Bind only store-postgresql-vault-auth in namespace pawbridge, audience vault.
path "secret/data/pawbridge/dev/store/postgresql" {
  capabilities = ["read"]
}
