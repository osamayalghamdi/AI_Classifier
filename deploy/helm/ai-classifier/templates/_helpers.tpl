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
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* Service name for an ingress path: a component name, or your own. */}}
{{- define "ai-classifier.pathServiceName" -}}
{{- if .name -}}
{{- .name -}}
{{- else -}}
{{- printf "%s-%s" (include "ai-classifier.fullname" .root) .component -}}
{{- end -}}
{{- end -}}

{{/* ConfigMap holding the frontend nginx config. */}}
{{- define "ai-classifier.frontendConfigMapName" -}}
{{- printf "%s-nginx" (include "ai-classifier.fullname" .) -}}
{{- end -}}

{{/* nginx config body from values.frontend.configMap.conf. The trailing
     newline is trimmed so the config hash stays stable across upgrades. */}}
{{- define "ai-classifier.frontendConf" -}}
{{- tpl .Values.frontend.configMap.conf . | trimSuffix "\n" -}}
{{- end -}}

{{/* Access control emitted inside the proxied (data-bearing) nginx
     locations: IP allow/deny and/or HTTP basic auth. Empty when neither is
     configured, so an unconfigured environment renders exactly as before. */}}
{{- define "ai-classifier.dataAuth" -}}
{{- with .Values.frontend.accessControl.allowlist }}
{{- range . }}
allow {{ . }};
{{- end }}
deny all;
{{- end }}
{{- if .Values.frontend.accessControl.basicAuth.enabled }}
auth_basic "{{ .Values.frontend.accessControl.basicAuth.realm }}";
auth_basic_user_file /etc/nginx/auth/htpasswd;
{{- end }}
{{- end -}}

{{/* Superseded: the database is externally managed, so the PG_* helpers
     that used to compute in-cluster host/user/db/port are gone. */}}
