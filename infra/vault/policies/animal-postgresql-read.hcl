# Bind only animal-postgresql-vault-auth in namespace pawbridge, audience vault.
path "secret/data/pawbridge/dev/animal/postgresql" {
  capabilities = ["read"]
}
