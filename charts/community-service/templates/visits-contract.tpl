{{- define "community-service.visitsContract" -}}
{{- if hasKey .Values.env "DAILY_VISITORS_SIGNING_KEY" -}}
{{- fail "Visitor signing key must be supplied by a dedicated Secret, never inline values" -}}
{{- end -}}
{{- if eq (get .Values.env "DAILY_VISITORS_ENABLED" | toString) "true" -}}
{{- if not .Values.visitsSecretRef -}}{{- fail "Enabled visitors require visitsSecretRef" -}}{{- end -}}
{{- if not .Values.postgresqlSecretRef -}}{{- fail "Visitors require PostgreSQL authentication" -}}{{- end -}}
{{- if ne (get .Values.env "SPRING_DATASOURCE_DRIVER_CLASS_NAME") "org.postgresql.Driver" -}}
{{- fail "Visitors require the PostgreSQL driver" -}}
{{- end -}}
{{- if ne (get .Values.env "DAILY_VISITORS_ALLOWED_ORIGINS") "https://www.pawbridge.kr" -}}
{{- fail "Production visitors require the exact approved www origin" -}}
{{- end -}}
{{- end -}}
{{- end -}}
