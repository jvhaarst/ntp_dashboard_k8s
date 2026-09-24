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
| pool score exporter (in this chart) | `python` | 9126 |
| client country exporter (in this chart) | `python` + `cturra/ntp` | 9127 |
| [geoip-api](https://github.com/observabilitystack/geoip-api) | `observabilitystack/geoip-api` | 8080 |

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
| `scrape.dropPodLabel` | `true` | Drops `pod` at scrape time so restarts do not fork every series |
| `poolExporter.enabled` | `false` | Poll the pool.ntp.org score feed |
| `poolExporter.servers` | `[]` | Public addresses as registered in the pool |
| `poolExporter.perMonitor` | `false` | Per-monitor series; ~600 of them, aggregates otherwise |

### Pool score exporter

chrony measures how well the local clock is disciplined. It cannot measure how
the server looks from the outside. The NTP Pool publishes exactly that, for
every server in the pool:

```
https://www.ntppool.org/scores/{ip}/log?limit=400&monitor=*
```

The feed is public and unauthenticated, in CSV with the columns
`ts_epoch,ts,offset,step,score,monitor_id,monitor_name,rtt,leap,error`. Around
190 monitoring stations report on a well-connected server, each with its own
offset and round-trip time.

A small standard-library Python exporter, shipped in this chart as a ConfigMap
and run on a stock `python` image, polls that feed every 5 minutes and exports:

| Metric | Meaning |
|---|---|
| `ntppool_score` | The pool's aggregate score, max 20 |
| `ntppool_monitors_total` | Monitoring stations reporting |
| `ntppool_monitor_score_min` / `_mean` | Worst and mean score across monitors |
| `ntppool_monitors_low_total` | Monitors below `lowScore` |
| `ntppool_monitor_offset_seconds{quantile}` | Offset as monitors see it, p05/p50/p95 |
| `ntppool_monitor_rtt_seconds{quantile}` | Round-trip time, p05/p50/p95 |
| `ntppool_monitor_abs_offset_max_seconds` | Worst absolute offset |
| `ntppool_up`, `ntppool_scrape_errors_total` | Feed reachability |

Per-monitor detail (`ntppool_monitor_score`, `_offset_seconds`, `_rtt_seconds`
labelled by monitor) is available via `perMonitor: true`, at roughly 600 series
instead of 17.

The pool rescores a few times an hour, so a shorter interval buys nothing.
`robots.txt` permits `/scores/` and disallows `/monitor/`.

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

### Client countries

chronyd already records every client that has asked it for the time, with a
request count and a drop count each. That is the same information a passive
packet capture would give, without needing `CAP_NET_RAW` on the pod that
disciplines the clock. A sidecar built from an image that carries `chronyc`
writes `chronyc -n clients` to a shared file; a standard-library Python
exporter resolves each address to a country and aggregates.

Client IP addresses are personal data. They are resolved in memory and never
exported, logged or written to disk — only per-country counts leave the pod.

Lookups are served in-cluster by `observabilitystack/geoip-api`, whose image
bundles a recent GeoLite2 database, so there is no MaxMind account, licence key
or refresh job. Addresses never leave the cluster.

| Metric | Meaning |
|---|---|
| `ntp_clients_total` | Clients in chronyd's log |
| `ntp_clients_by_country` | Clients per country |
| `ntp_client_requests_by_country` | NTP requests per country |
| `ntp_client_drops_by_country` | Rate-limited requests per country |
| `ntp_clients_without_country` | Addresses the database has no country for |
| `ntp_clients_geoip_failures_total` | Lookups where the database was unreachable |

Both geomaps use the `osm-standard` basemap. Grafana's built-in default is
CARTO, whose tile service now requires an API key and shows a warning in the
panel; OpenStreetMap needs neither key nor account. Tiles are fetched by the
viewer's browser, not by the cluster. Set `geomap.default_baselayer_config` in
`grafana.ini` if you would rather pin this instance-wide.

**The geoip-api image is a GraalVM native image and will not start on a 16 KiB
page-size kernel.** Every Raspberry Pi 5 (`-rpi-2712`) node fails immediately
with `Fatal error: Failed to create the main Isolate. (code 24)`. Pin it with
`geoipApi.nodeSelector` to a 4 KiB-page node — check with `getconf PAGESIZE`.

## Alerting

`rules.enabled` is `false` by default. The `VMRule` this chart can create needs
vmalert to evaluate it, and `victoria-metrics-k8s-stack` ships with
`vmalert.enabled: false`. Set `rules.enabled: true` only if vmalert is running.

When enabled, the `VMRule` provides the recording rule
`instance:chrony_clock_error_seconds:abs` and alerts for: exporter down, loss of
stratum 1, clock error above threshold, refclock unreachable, client log
overflow, GNSS fix lost, and too few satellites.
