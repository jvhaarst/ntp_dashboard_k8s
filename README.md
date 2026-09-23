# ntp_dashboard_k8s

Helm chart that monitors a chrony/GNSS stratum-1 NTP server from a k3s cluster
running VictoriaMetrics, and shows the result in Grafana.

The chart deploys two upstream exporters onto the node that owns the GNSS
receiver, registers them with the VictoriaMetrics operator, and ships the
dashboards that read them. No custom images are built.

| Component | Image | Port |
|---|---|---|
| [chrony_exporter](https://github.com/SuperQ/chrony_exporter) | `quay.io/superq/chrony-exporter` | 9123 |
| [gpsd-prometheus-exporter](https://github.com/brendanbank/gpsd-prometheus-exporter) | `ghcr.io/brendanbank/gpsd-prometheus-exporter` | 9015 |

## Helm repository

```
https://jvhaarst.github.io/ntp_dashboard_k8s
```

```bash
helm repo add ntp-dashboard https://jvhaarst.github.io/ntp_dashboard_k8s
helm repo update
helm install ntp-exporters ntp-dashboard/ntp-exporters -n monitoring
```

In Rancher, add the same URL under **Apps → Repositories** as an `http(s)`
repository; the chart then appears in the app catalogue and upgrades from there.

## Requirements

- A node running `chronyd` and `gpsd`, both listening on loopback. The pod uses
  `hostNetwork: true` to reach them, so ports 9123 and 9015 must be free there.
- The VictoriaMetrics operator, for the `VMPodScrape` and `VMRule` resources.
- Grafana with the dashboard sidecar, to pick up the dashboard ConfigMaps.

## Configuration

See `ntp-exporters/values.yaml`. The values most likely to need changing:

| Key | Default | Notes |
|---|---|---|
| `nodeSelector` | `kubernetes.io/hostname: ntp` | The node with the GNSS receiver |
| `tolerations` | `longhorn.io/exclude` | Match the taints on that node |
| `chronyExporter.collectors.clients` | `true` | Needs chrony >= 4.0 |
| `gpsdExporter.disableSatelliteMonitoring` | `false` | Per-satellite series carry the `gnssid` label |
| `gpsdExporter.geopoint` | disabled | Set `lat`/`lon` to the real antenna position to enable |
| `rules.refclockSourceName` | `PPS` | Name of the refclock that must stay reachable |

### Host preparation

`chronyc serverstats` reports `Client log records dropped`. If that counter
grows, `clientloglimit` in `chrony.conf` is too small for the number of clients
and `chrony_clients_connected` will undercount. Add a drop-in under
`/etc/chrony/conf.d/` and restart `chronyd` at a moment of your choosing:

```
clientloglimit 16777216
```

### Grafana

This chart only ships dashboards. Grafana itself is a values change on the
`victoria-metrics-k8s-stack` release. Set an admin password explicitly rather
than letting the chart generate one:

```bash
kubectl create secret generic grafana-admin -n monitoring \
  --from-literal=admin-user=admin \
  --from-literal=admin-password='<chosen>'
```

```yaml
grafana:
  enabled: true
  admin:
    existingSecret: grafana-admin
    userKey: admin-user
    passwordKey: admin-password
  persistence:
    enabled: true
    storageClassName: longhorn
    size: 2Gi
  grafana.ini:
    users:
      allow_sign_up: false
    auth.anonymous:
      enabled: false
defaultDashboards:
  enabled: true
```

## Dashboards

- **NTP server (chrony + GNSS)** — clock offset and error bound, oscillator
  discipline, per-source offsets and reachability, packets served and dropped,
  clients, and satellites per constellation. The clock error bound is computed
  inline, so it needs no recording rule.
- **GPSD** — the upstream dashboard shipped with gpsd-prometheus-exporter.

### Panels that stay empty by default

The upstream GPSD dashboard covers two exporter features this chart leaves off:

| Panels | Needs |
|---|---|
| Clock offset from PPS, and the three percentile panels | `gpsdExporter.ppsHistogram: true` |
| x-offset, y-offset and offset in meters to a stationary geo point | `gpsdExporter.geopoint.enabled: true` with real `lat`/`lon` |

Enabling geopoint is worthwhile on a fixed antenna: position scatter is a decent
antenna-health and spoofing signal, and it needs no extra privilege.

`ppsHistogram` is best left off. It requires `/dev/pps0` inside the container,
and the upstream README notes that it assumes the PPS signal is perfect and does
not measure synchronisation accuracy. chrony's PPS refclock already measures
that properly, and it shows up on the **Source offsets** panel of the other
dashboard.

## Alerting

`rules.enabled` is `false` by default. The `VMRule` this chart can create needs
vmalert to evaluate it, and `victoria-metrics-k8s-stack` ships with
`vmalert.enabled: false`. Set `rules.enabled: true` only if vmalert is running.

When enabled, the `VMRule` provides the recording rule
`instance:chrony_clock_error_seconds:abs` and alerts for: exporter down, loss of
stratum 1, clock error above threshold, refclock unreachable, client log
overflow, GNSS fix lost, and too few satellites.
