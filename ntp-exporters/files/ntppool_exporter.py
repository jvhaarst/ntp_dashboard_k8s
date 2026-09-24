#!/usr/bin/env python3
"""Export pool.ntp.org monitoring scores as Prometheus metrics.

The NTP Pool publishes, for every server in the pool, the score history and the
offset and round-trip time measured by each of its monitoring stations:

    https://www.ntppool.org/scores/{ip}/log?limit=N&monitor=*

That is the only outside-in view of an NTP server: chrony can say how well the
local clock is disciplined, but not how the server looks to the rest of the
world. This exporter turns that feed into metrics.

Roughly 190 monitors report on a well-connected server. Emitting a series per
monitor is ~600 churning series, so by default only aggregates are exported and
per-monitor detail is opt-in via NTPPOOL_PER_MONITOR.

Standard library only, so it runs in a stock python image with no build step.
"""

import csv
import io
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVERS = [s.strip() for s in os.environ.get("NTPPOOL_SERVERS", "").split(",") if s.strip()]
INTERVAL = int(os.environ.get("NTPPOOL_INTERVAL", "300"))
LIMIT = int(os.environ.get("NTPPOOL_LIMIT", "400"))
PORT = int(os.environ.get("NTPPOOL_PORT", "9126"))
PER_MONITOR = os.environ.get("NTPPOOL_PER_MONITOR", "false").lower() in ("1", "true", "yes")
LOW_SCORE = float(os.environ.get("NTPPOOL_LOW_SCORE", "10"))
BASE_URL = os.environ.get("NTPPOOL_BASE_URL", "https://www.ntppool.org")
USER_AGENT = os.environ.get("NTPPOOL_USER_AGENT", "ntppool-exporter/1.0 (+self-hosted monitoring)")
TIMEOUT = int(os.environ.get("NTPPOOL_TIMEOUT", "30"))

# The pool's own aggregate scorer. Its rows carry the authoritative score and no
# offset/rtt of their own, so it is excluded from the per-monitor statistics.
AGGREGATE_MONITOR = "recentmedian"

_lock = threading.Lock()
_payload = "# no data collected yet\n"
_errors = {}


def esc(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def fetch(server):
    url = "{}/scores/{}/log?{}".format(
        BASE_URL, urllib.parse.quote(server), urllib.parse.urlencode({"limit": LIMIT, "monitor": "*"})
    )
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def newest_per_monitor(body):
    """Rows arrive newest-first, so the first row seen for a monitor is current."""
    seen = {}
    for row in csv.DictReader(io.StringIO(body)):
        name = (row.get("monitor_name") or "").strip()
        if name and name not in seen:
            seen[name] = row
    return seen


def to_float(row, key):
    raw = (row.get(key) or "").strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def quantiles(values):
    """Return 5th, 50th and 95th percentile. statistics.quantiles needs n>=2."""
    if not values:
        return {}
    ordered = sorted(values)
    if len(ordered) == 1:
        return {"0.05": ordered[0], "0.5": ordered[0], "0.95": ordered[0]}

    def pick(frac):
        idx = min(len(ordered) - 1, max(0, int(round(frac * (len(ordered) - 1)))))
        return ordered[idx]

    return {"0.05": pick(0.05), "0.5": statistics.median(ordered), "0.95": pick(0.95)}


def collect(server, out):
    label = 'server="{}"'.format(esc(server))
    started = time.time()
    try:
        rows = newest_per_monitor(fetch(server))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
        _errors[server] = _errors.get(server, 0) + 1
        out.append("ntppool_up{{{}}} 0".format(label))
        out.append("ntppool_scrape_errors_total{{{}}} {}".format(label, _errors[server]))
        print("scrape failed for {}: {}".format(server, exc), file=sys.stderr, flush=True)
        return

    out.append("ntppool_up{{{}}} 1".format(label))
    out.append("ntppool_scrape_errors_total{{{}}} {}".format(label, _errors.get(server, 0)))
    out.append("ntppool_scrape_duration_seconds{{{}}} {:.6f}".format(label, time.time() - started))

    aggregate = rows.get(AGGREGATE_MONITOR)
    if aggregate:
        score = to_float(aggregate, "score")
        if score is not None:
            out.append("ntppool_score{{{}}} {}".format(label, score))
        ts = to_float(aggregate, "ts_epoch")
        if ts is not None:
            out.append("ntppool_score_timestamp_seconds{{{}}} {}".format(label, ts))

    monitors = {k: v for k, v in rows.items() if k != AGGREGATE_MONITOR}
    out.append("ntppool_monitors_total{{{}}} {}".format(label, len(monitors)))

    scores, offsets, rtts = [], [], []
    for name, row in monitors.items():
        sc, off, rtt = to_float(row, "score"), to_float(row, "offset"), to_float(row, "rtt")
        if sc is not None:
            scores.append(sc)
        if off is not None:
            offsets.append(off)
        if rtt is not None:
            rtts.append(rtt / 1000.0)  # the feed reports rtt in milliseconds
        if PER_MONITOR:
            ml = '{},monitor="{}"'.format(label, esc(name))
            if sc is not None:
                out.append("ntppool_monitor_score{{{}}} {}".format(ml, sc))
            if off is not None:
                out.append("ntppool_monitor_offset_seconds{{{}}} {}".format(ml, off))
            if rtt is not None:
                out.append("ntppool_monitor_rtt_seconds{{{}}} {}".format(ml, rtt / 1000.0))

    if scores:
        out.append("ntppool_monitor_score_min{{{}}} {}".format(label, min(scores)))
        out.append("ntppool_monitor_score_mean{{{}}} {:.6f}".format(label, statistics.fmean(scores)))
        low = len([s for s in scores if s < LOW_SCORE])
        out.append("ntppool_monitors_low_total{{{}}} {}".format(label, low))
    for name, values in (("offset_seconds", offsets), ("rtt_seconds", rtts)):
        for q, v in quantiles(values).items():
            out.append(
                'ntppool_monitor_{}{{{},quantile="{}"}} {}'.format(name, label, q, v)
            )
    if offsets:
        out.append("ntppool_monitor_abs_offset_max_seconds{{{}}} {}".format(label, max(abs(o) for o in offsets)))


def render():
    out = [
        "# HELP ntppool_up Whether the last fetch of the pool score feed succeeded.",
        "# TYPE ntppool_up gauge",
        "# HELP ntppool_score Score from the pool's aggregate scorer (max 20).",
        "# TYPE ntppool_score gauge",
        "# HELP ntppool_monitors_total Monitoring stations that reported on this server.",
        "# TYPE ntppool_monitors_total gauge",
        "# HELP ntppool_monitor_offset_seconds Offset seen by monitors, by quantile.",
        "# TYPE ntppool_monitor_offset_seconds gauge",
        "# HELP ntppool_monitor_rtt_seconds Round-trip time seen by monitors, by quantile.",
        "# TYPE ntppool_monitor_rtt_seconds gauge",
    ]
    for server in SERVERS:
        collect(server, out)
    return "\n".join(out) + "\n"


def poll_forever():
    global _payload
    while True:
        try:
            body = render()
        except Exception as exc:  # never let the poll thread die
            body = "# collection failed: {}\n".format(exc)
            print("collection failed: {}".format(exc), file=sys.stderr, flush=True)
        with _lock:
            _payload = body
        time.sleep(INTERVAL)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] not in ("/metrics", "/"):
            self.send_error(404)
            return
        with _lock:
            body = _payload.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # the access log is noise; scrape failures surface as ntppool_up


def main():
    if not SERVERS:
        print("NTPPOOL_SERVERS is empty; nothing to poll", file=sys.stderr, flush=True)
    threading.Thread(target=poll_forever, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
