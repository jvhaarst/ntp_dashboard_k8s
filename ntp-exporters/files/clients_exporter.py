#!/usr/bin/env python3
"""Aggregate chrony's client log into per-country metrics.

chronyd already records every client that has asked it for the time, with a
request count and a drop count each. That is the same data a passive packet
capture would produce, without needing CAP_NET_RAW on the host that disciplines
the clock: a sidecar writes `chronyc -n clients` to a shared file and this
process reads it.

Client IP addresses are personal data. They are resolved to a country in memory
and never exported, logged or written to disk; only per-country aggregates
leave this process.

Standard library only, so it runs on a stock python image with no build step.
"""

import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CLIENTS_FILE = os.environ.get("CLIENTS_FILE", "/var/lib/ntp-clients/clients.txt")
GEOIP_URL = os.environ.get("GEOIP_URL", "http://127.0.0.1:8080")
PORT = int(os.environ.get("CLIENTS_PORT", "9127"))
INTERVAL = int(os.environ.get("CLIENTS_INTERVAL", "300"))
# Cap lookups per cycle so the first fill (14k+ clients) cannot block for ever.
MAX_LOOKUPS = int(os.environ.get("CLIENTS_MAX_LOOKUPS", "3000"))
GEOIP_TIMEOUT = float(os.environ.get("GEOIP_TIMEOUT", "2"))
UNKNOWN = os.environ.get("CLIENTS_UNKNOWN_LABEL", "unknown")

_lock = threading.Lock()
_payload = "# no data collected yet\n"
_country = {}           # ip -> country code, the only thing kept about an address
_geoip_failures = 0
_parse_errors = 0


def esc(v):
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def parse_clients(path):
    """Yield (ip, ntp_requests, ntp_drops) from `chronyc -n clients` output.

    Columns: Hostname NTP Drop Int IntL Last Cmd Drop Int Last
    """
    rows = []
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("=") or line.startswith("Hostname"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue
                ip = parts[0]
                try:
                    rows.append((ip, int(parts[1]), int(parts[2])))
                except ValueError:
                    continue
    except FileNotFoundError:
        return None
    return rows


class GeoIP:
    """Small keep-alive client. One connection beats 14k fresh handshakes."""

    def __init__(self, base):
        u = urllib.parse.urlparse(base)
        self.host = u.hostname
        self.port = u.port or 80
        self.conn = None

    def _connect(self):
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=GEOIP_TIMEOUT)

    def country(self, ip):
        """Return (reached, country). A reachable database that simply has no
        country for an address (anycast, for instance) is not a failure."""
        for attempt in (1, 2):  # one retry, since a pooled connection can go stale
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("GET", "/" + urllib.parse.quote(ip))
                resp = self.conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    return (False, None)
                return (True, json.loads(body).get("country") or None)
            except (http.client.HTTPException, OSError, socket.timeout, ValueError):
                try:
                    if self.conn:
                        self.conn.close()
                except Exception:
                    pass
                self.conn = None
                if attempt == 2:
                    return (False, None)
        return (False, None)


def collect(geoip):
    global _geoip_failures, _parse_errors
    rows = parse_clients(CLIENTS_FILE)
    out = []
    if rows is None:
        _parse_errors += 1
        out.append("ntp_clients_up 0")
        out.append("ntp_clients_parse_errors_total {}".format(_parse_errors))
        return "\n".join(out) + "\n"

    looked_up = 0
    for ip, _req, _drop in rows:
        if ip in _country or looked_up >= MAX_LOOKUPS:
            continue
        reached, cc = geoip.country(ip)
        looked_up += 1
        if not reached:
            _geoip_failures += 1
            continue  # leave it uncached so the next cycle retries
        # Cache the miss too: a second lookup of an address the database does
        # not know costs the same and returns the same nothing.
        _country[ip] = cc or UNKNOWN

    clients = defaultdict(int)
    requests = defaultdict(int)
    drops = defaultdict(int)
    for ip, req, drop in rows:
        cc = _country.get(ip, UNKNOWN)
        clients[cc] += 1
        requests[cc] += req
        drops[cc] += drop

    out.append("# HELP ntp_clients_by_country Clients in chronyd's log, by country.")
    out.append("# TYPE ntp_clients_by_country gauge")
    out.append("# HELP ntp_client_requests_by_country NTP requests counted by chronyd, by country.")
    out.append("# TYPE ntp_client_requests_by_country gauge")
    out.append("ntp_clients_up 1")
    out.append("ntp_clients_total {}".format(len(rows)))
    out.append("ntp_clients_countries_total {}".format(len([c for c in clients if c != UNKNOWN])))
    out.append("ntp_clients_geoip_cache_size {}".format(len(_country)))
    out.append("# HELP ntp_clients_geoip_failures_total Lookups where the database could not be reached.")
    out.append("ntp_clients_geoip_failures_total {}".format(_geoip_failures))
    out.append("ntp_clients_without_country {}".format(len([c for c in _country.values() if c == UNKNOWN])))
    out.append("ntp_clients_lookups_last_cycle {}".format(looked_up))
    out.append("ntp_clients_parse_errors_total {}".format(_parse_errors))
    out.append("ntp_clients_last_collection_timestamp_seconds {}".format(int(time.time())))
    for cc in sorted(clients):
        lbl = 'country="{}"'.format(esc(cc))
        out.append("ntp_clients_by_country{{{}}} {}".format(lbl, clients[cc]))
        out.append("ntp_client_requests_by_country{{{}}} {}".format(lbl, requests[cc]))
        out.append("ntp_client_drops_by_country{{{}}} {}".format(lbl, drops[cc]))
    return "\n".join(out) + "\n"


def poll_forever():
    global _payload
    geoip = GeoIP(GEOIP_URL)
    while True:
        started = time.time()
        try:
            body = collect(geoip)
            body += "ntp_clients_collect_duration_seconds {:.3f}\n".format(time.time() - started)
        except Exception as exc:  # the poll thread must never die
            body = "ntp_clients_up 0\n"
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
        pass


def main():
    threading.Thread(target=poll_forever, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
