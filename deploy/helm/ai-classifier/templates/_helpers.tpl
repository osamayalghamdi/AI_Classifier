{{- define "ai-classifier.name" -}}
{{- .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ai-classifier.fullname" -}}
{{- printf "%s-%s" .Release.Name (include "ai-classifier.name" .) | trunc 63 | trimSuffix "-" -}}
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

{{- define "ai-classifier.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
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
