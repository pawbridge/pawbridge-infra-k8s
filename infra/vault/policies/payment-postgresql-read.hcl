# Bind only payment-postgresql-vault-auth in namespace pawbridge, audience vault.
path "secret/data/pawbridge/dev/payment/postgresql" {
  capabilities = ["read"]
}
