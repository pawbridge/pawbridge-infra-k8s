# Exact local development paths only. The legacy pawbridge/dev prefix is production.
path "secret/data/pawbridge/local-dev/runtime" {
  capabilities = ["read"]
}
path "secret/data/pawbridge/local-dev/google" {
  capabilities = ["read"]
}
path "sys/capabilities-self" {
  capabilities = ["update"]
}
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/token/revoke-self" {
  capabilities = ["update"]
}
