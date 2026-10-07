# Read only the dedicated Community YouTube KV-v2 path.
path "secret/data/pawbridge/dev/community/youtube" {
  capabilities = ["read"]
}

path "secret/metadata/pawbridge/dev/community/youtube" {
  capabilities = ["read"]
}

# VSO uses these endpoints to manage only its own short-lived Vault token.
# Do not add the default policy, token creation, or non-self token operations.
path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
