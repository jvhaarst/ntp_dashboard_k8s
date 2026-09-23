# Monitoring a stratum-1 NTP server from k3s

**Date:** 2026-09-23
**Status:** approved, ready for implementation

## Purpose

`ntp.local` serves the public NTP pool as a stratum-1 server disciplined by a
u-blox M8 GNSS receiver and a PPS signal. Its behaviour is currently visible
only by logging in and running `chronyc`. The goal is to record how well it
keeps time and how well it serves clients, store that in the VictoriaMetrics
instance already running in the k3s cluster, and present it in Grafana.

Success means: clock accuracy, reference-clock health, upstream peer agreement,
client load and GNSS constellation health are all queryable over a 30-day
window and visible on a dashboard, with alerts for the failure modes that
matter.

## Context

The decisive fact is that `ntp.local` is itself a node in the k3s cluster
(node `ntp`, 192.168.0.252, `k3s-agent` active). Metrics therefore do not need
to cross a trust or network boundary; the exporters can be ordinary workloads
scheduled onto that node.

What already exists:

- VictoriaMetrics via the operator in namespace `monitoring`: `vmsingle` and
  `vmagent` from the `victoria-metrics` Helm release, 30-day retention on 20Gi,
  reachable at `vm.vanhaarst.net`. The `VMPodScrape`, `VMRule` and related CRDs
  are installed.
- A `prometheus-node-exporter` DaemonSet on every node including `ntp`, running
  with `hostNetwork` and tolerating all taints. It already supplies
  `node_timex_offset_seconds`, `node_timex_maxerror_seconds`,
  `node_timex_frequency_adjustment_ratio`, `node_timex_sync_status` and
  `node_timex_pps_*`. The textfile collector is not enabled.
- chrony 4.0 and gpsd 3.22 on the host, both listening on loopback only:
  chronyd on UDP `127.0.0.1:323` and `[::1]:323`, gpsd on `127.0.0.1:2947`.
  A chronyd command socket exists at `/run/chrony/chronyd.sock`.
- Grafana is not installed anywhere. The `victoria-metrics` release has
  `grafana.enabled: false` and `defaultDashboards.enabled: false`.
- Chart repositories are added to Rancher as ClusterRepos, including `vm`.
  Personal charts follow the pattern `https://jvhaarst.github.io/<repo>`.

Constraints discovered during exploration:

- Node `ntp` carries the taint `longhorn.io/exclude=true:NoSchedule`, so any
  new workload there needs a matching toleration.
- Two StorageClasses are both marked default (`local-path` and `longhorn`), so
  any new PVC must name its class explicitly.
- `chronyc serverstats` reports 238391 client log records dropped against
  1595418 NTP packets received. `clientloglimit` is unset, so the default
  512 KiB holds roughly 4096 records and client statistics undercount.
- The host has 1846 MB of memory, of which about 1335 MB is available.

## Design

### Data flow

    ntp.local (k3s node "ntp", hostNetwork)
      chronyd  --UDP [::1]:323-------> chrony-exporter :9123 --\
      gpsd     --JSON localhost:2947-> gpsd-exporter   :9015 --+-> vmagent -> vmsingle -> Grafana
      (existing node-exporter :9100, node_timex_*)            --/

### Packaging

A Helm chart named `ntp-exporters`, published from the GitHub repository
`jvhaarst/ntp_dashboard_k8s` to GitHub Pages at
`https://jvhaarst.github.io/ntp_dashboard_k8s`, added to Rancher as a
ClusterRepo. This follows the existing pattern used by `zeildashboard_k8s`,
`charger_dashboard_k8s` and the other personal chart repositories, so upgrades
happen through Rancher like everything else.

### Workload

One DaemonSet with two containers, `nodeSelector` pinned to
`kubernetes.io/hostname: ntp`, `hostNetwork: true`,
`dnsPolicy: ClusterFirstWithHostNet`, and a toleration for the longhorn taint.
Both daemons listen on loopback only, so sharing the host network namespace is
what makes them reachable. Ports 9123 and 9015 are free on that host.

**chrony_exporter** (`quay.io/superq/chrony-exporter:v0.14.0`) with
`--chrony.address=[::1]:323` and the `tracking`, `sources`, `sourcestats`,
`serverstats` and `clients` collectors. It runs unprivileged and non-root:
reaching chronyd over loopback UDP needs no capabilities.
`--collector.sources.with-ntpdata` is deliberately left off, because it
requires the unix socket, root, and `--collector.chmod-socket`. It can be
enabled later if per-source `ntpdata` proves worth the privilege.

**gpsd-prometheus-exporter** (`ghcr.io/brendanbank/gpsd-prometheus-exporter:1.1.19`,
multi-arch including linux/arm64). The image's default command is
`entrypoint.sh`, which constructs its arguments from environment variables and
unconditionally passes `--offset-from-geopoint` using coordinates baked into
the Dockerfile. The chart therefore overrides the command and invokes
`python3 /app/gpsd_exporter.py` directly, so the flags in the manifest are the
complete argument list and geopoint tracking is genuinely optional.

Per-satellite monitoring stays enabled. Those series carry the `gnssid` label,
which is what makes per-constellation health — including Galileo — visible at
all. `--pps-histogram` stays disabled: it needs `/dev/pps0` inside the
container, and its own documentation notes that it assumes a perfect PPS signal
and does not measure synchronisation accuracy. chrony's PPS refclock is the
authoritative measurement.

### Scrape configuration

A `VMPodScrape` in `monitoring` selecting the DaemonSet's pod labels, covering
both named ports at a 30-second interval.

### Rules

A `VMRule` containing the recording rule
`instance:chrony_clock_error_seconds:abs`, the bound on clock accuracy
documented in the chrony manual, plus alerts for: the exporter losing contact
with chronyd, loss of stratum 1, clock error above threshold, the PPS refclock
becoming unreachable, chronyd dropping client log records, loss of GNSS fix,
and too few satellites in the navigation solution.

The client-log alert is included specifically because that counter is already
growing on this host.

### Dashboards

Two ConfigMaps labelled `grafana_dashboard: "1"` for the Grafana sidecar:

- **NTP server (chrony + GNSS)**, written for this deployment: clock offset and
  error bound, root delay and dispersion, oscillator frequency and skew,
  per-source offsets and reachability, NTP packets served and dropped, clients
  in chronyd's log, client-log drop rate, and satellites per constellation.
- **GPSD**, the dashboard shipped upstream with the gpsd exporter.

### Grafana

Enabled as a values change on the existing `victoria-metrics` release rather
than as a separate release, so the datasource and dashboard sidecar are wired
by the chart. No new Rancher repository is needed, because Grafana is a bundled
dependency of `victoria-metrics-k8s-stack` and the `vm` ClusterRepo is already
present.

The admin password is set explicitly through a pre-created Secret referenced by
`grafana.admin.existingSecret`. It is never chart-generated, never written into
values, and never committed. Anonymous access and sign-up are both disabled,
because the ingress is public. Persistence names `storageClassName: longhorn`
explicitly, since two default StorageClasses exist.

### Host change

A `clientloglimit` drop-in under `/etc/chrony/conf.d/`, raising the limit from
the 512 KiB default to 16 MiB. This is negligible against available memory and
stops the client statistics from undercounting. It requires a `chronyd`
restart, which on a server answering the public pool should be done
deliberately rather than as a side effect of the rollout.

## Rejected alternatives

**A host-installed systemd exporter scraped by `VMStaticScrape`.** Workable,
but it puts the configuration outside the cluster and outside the upgrade path
that every other component here follows.

**The node-exporter textfile collector.** Avoids new scrape targets, but
requires changing the shared DaemonSet across all four nodes, and gives
resolution bounded by a timer rather than by the scrape interval.

**Telegraf.** Already installed on the host, though currently in a `failed`
state. Rejected because it would introduce a second metrics pipeline alongside
the Prometheus-shaped one the cluster already runs.

**A custom GNSS exporter.** Initially planned, then dropped: a maintained
upstream exporter exists, publishes multi-arch images including arm64, and
ships its own Grafana dashboard.

## Verification

1. `curl 192.168.0.252:9123/metrics` and `curl 192.168.0.252:9015/metrics` on
   the host return metrics.
2. Both targets report `up` in vmagent.
3. `chrony_tracking_last_offset_seconds` and `gpsd_sat_used` are queryable in
   vmsingle.
4. Both dashboards render with data.
5. Grafana rejects a wrong admin password and does not permit anonymous access.
