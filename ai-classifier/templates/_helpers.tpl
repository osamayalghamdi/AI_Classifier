{{- define "ai-classifier.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ai-classifier.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "ai-classifier.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "ai-classifier.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
app.kubernetes.io/name: {{ include "ai-classifier.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ai-classifier.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ai-classifier.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ai-classifier.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "ai-classifier.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "ai-classifier.image" -}}
{{- $tag := default .Chart.AppVersion .Values.image.tag -}}
{{- printf "%s:%s" .Values.image.repository $tag -}}
{{- end -}}

{{/* Postgres host as seen by the API */}}
{{- define "ai-classifier.pgHost" -}}
{{- if .Values.postgres.enabled -}}
{{- printf "%s-postgres" (include "ai-classifier.fullname" .) -}}
{{- else -}}
{{- required "postgres.external.host is required when postgres.enabled=false" .Values.postgres.external.host -}}
{{- end -}}
{{- end -}}

{{- define "ai-classifier.pgUser" -}}
{{- if .Values.postgres.enabled }}{{ .Values.postgres.auth.username }}{{ else }}{{ .Values.postgres.external.username }}{{ end -}}
{{- end -}}

{{- define "ai-classifier.pgDatabase" -}}
{{- if .Values.postgres.enabled }}{{ .Values.postgres.auth.database }}{{ else }}{{ .Values.postgres.external.database }}{{ end -}}
{{- end -}}

{{- define "ai-classifier.pgPort" -}}
{{- if .Values.postgres.enabled }}5432{{ else }}{{ .Values.postgres.external.port }}{{ end -}}
{{- end -}}
