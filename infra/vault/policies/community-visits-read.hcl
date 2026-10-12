# Dedicated production visitor key. No other Community secrets are readable.
path "secret/data/pawbridge/prod/community/visits" {
  capabilities = ["read"]
}
path "secret/metadata/pawbridge/prod/community/visits" {
  capabilities = ["read"]
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
