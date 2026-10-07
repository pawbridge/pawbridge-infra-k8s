# Read only the dedicated Community YouTube KV-v2 path.
path "secret/data/pawbridge/dev/community/youtube" {
  capabilities = ["read"]
}

path "secret/metadata/pawbridge/dev/community/youtube" {
  capabilities = ["read"]
}
