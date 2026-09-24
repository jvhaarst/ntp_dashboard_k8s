{{- define "ntp-exporters.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ntp-exporters.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "ntp-exporters.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "ntp-exporters.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ntp-exporters.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "ntp-exporters.labels" -}}
helm.sh/chart: {{ include "ntp-exporters.chart" . }}
{{ include "ntp-exporters.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: ntp-exporters
{{- end }}

{{/*
Labels for the pod template only. Deliberately excludes helm.sh/chart and
app.kubernetes.io/version: both change on every chart release, and anything in
the pod template is part of the pod spec, so a dashboard-only version bump
would otherwise roll the exporters and put a gap in the metrics.
*/}}
{{- define "ntp-exporters.podLabels" -}}
{{ include "ntp-exporters.selectorLabels" . }}
app.kubernetes.io/part-of: ntp-exporters
{{- end }}
