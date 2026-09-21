# Bind only community-postgresql-vault-auth in namespace pawbridge, audience vault.
path "secret/data/pawbridge/dev/community/postgresql" {
  capabilities = ["read"]
}
