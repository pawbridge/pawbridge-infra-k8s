{{- define "store-service.postgresqlContract" -}}
{{- if .Values.postgresqlSecretRef -}}
{{- if .Values.mysqlSecretRef -}}{{ fail "PostgreSQL and MySQL Secret references are mutually exclusive" }}{{- end -}}
{{- if not (regexMatch "^sha256:[a-f0-9]{64}$" (default "" .Values.image.digest)) -}}{{ fail "PostgreSQL requires an explicitly selected migration-capable image digest" }}{{- end -}}
{{- if not (has "postgresql" (splitList "," (default "" .Values.env.SPRING_PROFILES_ACTIVE))) -}}{{ fail "PostgreSQL profile required" }}{{- end -}}
{{- if not (hasPrefix "jdbc:postgresql://" (default "" .Values.env.SPRING_DATASOURCE_URL)) -}}{{ fail "PostgreSQL JDBC URL required" }}{{- end -}}
{{- if ne (default "" .Values.env.SPRING_DATASOURCE_DRIVER_CLASS_NAME) "org.postgresql.Driver" -}}{{ fail "PostgreSQL driver required" }}{{- end -}}
{{- if ne (default "" .Values.env.SPRING_JPA_PROPERTIES_HIBERNATE_DIALECT) "org.hibernate.dialect.PostgreSQLDialect" -}}{{ fail "PostgreSQL dialect required" }}{{- end -}}
{{- if ne (default "" .Values.env.SPRING_JPA_HIBERNATE_DDL_AUTO) "validate" -}}{{ fail "PostgreSQL application DDL must be validate" }}{{- end -}}
{{- if hasKey .Values.env "SPRING_DATASOURCE_PASSWORD" -}}{{ fail "Database password must come only from the selected Secret" }}{{- end -}}
{{- if hasKey .Values.env "SPRING_DATASOURCE_HIKARI_CONNECTION_INIT_SQL" -}}{{ fail "Hikari connection-init-sql requires canonical CONNECTIONINITSQL environment spelling" }}{{- end -}}
{{- $pool := int (default "0" .Values.env.SPRING_DATASOURCE_HIKARI_MAXIMUMPOOLSIZE) -}}
{{- if or (lt $pool 1) (gt $pool 5) -}}{{ fail "PostgreSQL service connection budget exceeded" }}{{- end -}}
{{- if or (lt (int .Values.replicaCount) 0) (gt (int .Values.replicaCount) 1) -}}{{ fail "PostgreSQL connection budget permits at most one steady replica per service" }}{{- end -}}
{{- $idle := int (default "0" .Values.env.SPRING_DATASOURCE_HIKARI_MINIMUMIDLE) -}}
{{- if or (lt $idle 0) (gt $idle $pool) -}}{{ fail "PostgreSQL minimum idle must fit the service pool budget" }}{{- end -}}
{{- if .Values.searchPreflight.enabled -}}{{ fail "PostgreSQL must disable the Elasticsearch search preflight Job" }}{{- end -}}
{{- if .Values.autoscaling.enabled -}}{{ fail "PostgreSQL migration requires bounded replicas; HPA must be disabled" }}{{- end -}}
{{- if or .Values.elasticsearchSecretRef .Values.elasticsearchCaSecretRef -}}{{ fail "PostgreSQL must not retain Elasticsearch credentials or CA mounts" }}{{- end -}}
{{- end -}}
{{- end -}}
