{{- define "community-service.chatContract" -}}
{{- if eq (toString (default "false" .Values.env.MEMBER_CHAT_ENABLED)) "true" -}}
{{- if not .Values.redisSecretRef -}}{{ fail "Member chat requires a Redis authentication Secret reference" }}{{- end -}}
{{- if or (hasKey .Values.env "SPRING_DATA_REDIS_PASSWORD") (hasKey .Values.env "SPRING_REDIS_PASSWORD") -}}{{ fail "Redis password must come only from the selected Secret" }}{{- end -}}
{{- if .Values.env.SPRING_DATA_REDIS_URL -}}{{ fail "Member chat requires Redis host and Secret password, not a URL that overrides credentials" }}{{- end -}}
{{- if not .Values.env.SPRING_DATA_REDIS_HOST -}}{{ fail "Member chat requires an explicit Redis host" }}{{- end -}}
{{- if not (regexMatch "^[a-z0-9-]{1,40}$" (default "" .Values.env.MEMBER_CHAT_NAMESPACE)) -}}{{ fail "Member chat requires an explicit Redis namespace" }}{{- end -}}
{{- $origins := splitList "," (default "" .Values.env.MEMBER_CHAT_ALLOWED_ORIGINS) -}}
{{- range $origins -}}
{{- if not (regexMatch "^https?://[a-zA-Z0-9.:-]+$" (trim .)) -}}{{ fail "Member chat requires explicit HTTP origins without wildcards" }}{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
