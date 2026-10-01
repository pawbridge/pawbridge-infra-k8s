# 운영 Vault 적용은 별도 승인 후에만 한다. 백업 R2 키 한 경로에만 읽기 허용.
path "secret/data/pawbridge/dev/postgresql/backup-r2" {
  capabilities = ["read"]
}
