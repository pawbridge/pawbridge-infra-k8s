# Bind only user-postgresql-vault-auth in namespace pawbridge, audience vault.
path "secret/data/pawbridge/dev/user/postgresql" {
  capabilities = ["read"]
}
