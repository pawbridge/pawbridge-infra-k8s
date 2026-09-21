# Bind only payment-postgresql-cdc-vault-auth in namespace kafka, audience vault.
path "secret/data/pawbridge/dev/payment/postgresql-cdc" {
  capabilities = ["read"]
}
