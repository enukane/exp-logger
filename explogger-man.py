#!/usr/bin/env python3
"""
explogger-man - ex-logger manager / dashboard

targets ファイルに記載された explogger-clt を 1 Hz でポーリングし、
Web UI で各ホストの位置・状態を地図上に表示する。
"""

import argparse
import json
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


# ===================================================================
# Poller: 各 clt を 1 Hz でポーリング
# ===================================================================

class Poller:
    """targets リストの各 clt に対して並列に /status を取得し続ける"""

    def __init__(self, targets: list[dict], timeout: float = 0.8):
        # targets: [{"name": "...", "url": "http://host:port"}, ...]
        self.targets = targets
        self.timeout = timeout
        # {name: {"status": dict|None, "error": str|None, "fetched_at": str}}
        self.results: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.results.items()}

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.monotonic()
            threads = []
            for tgt in self.targets:
                th = threading.Thread(
                    target=self._fetch_one, args=(tgt,), daemon=True
                )
                th.start()
                threads.append(th)
            for th in threads:
                th.join(timeout=self.timeout + 0.5)
            elapsed = time.monotonic() - t0
            sleep_time = max(0, 1.0 - elapsed)
            if sleep_time > 0:
                self._stop.wait(sleep_time)

    def _fetch_one(self, tgt: dict):
        name = tgt["name"]
        address = tgt["address"]
        url = tgt["url"].rstrip("/") + "/status"
        now = datetime.now(timezone.utc).isoformat()
        try:
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
            with self._lock:
                self.results[name] = {
                    "status": data,
                    "address": address,
                    "error": None,
                    "fetched_at": now,
                }
        except Exception as e:
            with self._lock:
                self.results[name] = {
                    "status": None,
                    "address": address,
                    "error": str(e),
                    "fetched_at": now,
                }


# ===================================================================
# Web UI handler
# ===================================================================

STALE_THRESHOLD_SEC = 10

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>explogger-man dashboard</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: monospace; background:#1a1a2e; color:#eee; }
#map { width:100%; height:55vh; }
#panel { padding:12px; overflow-y:auto; max-height:45vh; }
.card { background:#16213e; border-radius:6px; padding:10px 14px; margin-bottom:8px; }
.card h3 { font-size:14px; margin-bottom:4px; }
.ok { border-left: 4px solid #0f0; }
.warn { border-left: 4px solid #ff0; }
.err { border-left: 4px solid #f33; }
.badge { display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; margin-left:6px; }
.badge-ok { background:#0a4; }
.badge-warn { background:#a80; }
.badge-err { background:#a22; }
pre { font-size:11px; white-space:pre-wrap; word-break:break-all; color:#aac; margin-top:4px; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel"><p>Loading...</p></div>
<script>
const STALE_SEC = """ + str(STALE_THRESHOLD_SEC) + r""";
const map = L.map('map').setView([35.68, 139.76], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
}).addTo(map);

const markers = {};
const lastKnownPos = {};  // {name: {lat, lon}}

function iconColor(color) {
    // simple colored circle marker
    return L.divIcon({
        className: '',
        html: `<div style="width:14px;height:14px;border-radius:50%;background:${color};border:2px solid #fff;"></div>`,
        iconSize: [14, 14],
        iconAnchor: [7, 7],
    });
}

function statusBadge(cls, text) {
    return `<span class="badge badge-${cls}">${text}</span>`;
}

function isStale(ts) {
    if (!ts) return true;
    const d = new Date(ts);
    if (isNaN(d)) return true;
    return (Date.now() - d.getTime()) > STALE_SEC * 1000;
}

// Known timestamp field names (tried in order)
const TS_FIELDS = ['system_timestamp', 'timestamp', 'time'];

function findTimestamp(obj) {
    if (!obj) return null;
    for (const f of TS_FIELDS) {
        if (obj[f]) return obj[f];
    }
    return null;
}

function renderHost(name, info) {
    const s = info.status;
    const err = info.error;
    const address = info.address || name;
    // clt の応答に hostname があればそれを使う。なければ targets の name (=IP)
    const displayHostname = (s && s.hostname) ? s.hostname : name;
    const displayLabel = `${displayHostname}(${address})`;

    let cls = 'ok';
    let badges = '';
    let lat = null, lon = null;

    if (err || !s) {
        cls = 'err';
        badges += statusBadge('err', 'NO RESPONSE');
    } else {
        const dataEntries = s.data || {};

        // Check each data source
        for (const [key, val] of Object.entries(dataEntries)) {
            const label = key.toUpperCase();
            if (!val) {
                badges += statusBadge('warn', `${label} N/A`);
                if (cls !== 'err') cls = 'warn';
            } else {
                const ts = findTimestamp(val);
                if (!ts) {
                    badges += statusBadge('warn', `${label} NO TS`);
                    if (cls !== 'err') cls = 'warn';
                } else if (isStale(ts)) {
                    badges += statusBadge('warn', `${label} STALE`);
                    if (cls !== 'err') cls = 'warn';
                } else {
                    badges += statusBadge('ok', `${label} OK`);
                }
            }
        }

        if (Object.keys(dataEntries).length === 0) {
            badges += statusBadge('warn', 'NO DATA');
            if (cls !== 'err') cls = 'warn';
        }

        // Extract GPS position (look for "gps" key in data)
        const gps = dataEntries.gps;
        if (gps) {
            lat = gps.latitude;
            lon = gps.longitude;
        }
    }

    // Update last known position
    if (lat != null && lon != null) {
        lastKnownPos[name] = { lat, lon };
    }

    // map marker (use last known position if current is unavailable)
    const pos = (lat != null && lon != null) ? { lat, lon } : lastKnownPos[name];
    const markerColor = cls === 'ok' ? '#0f0' : cls === 'warn' ? '#ff0' : '#f33';
    if (pos) {
        if (markers[name]) {
            markers[name].setLatLng([pos.lat, pos.lon]);
            markers[name].setIcon(iconColor(markerColor));
            markers[name].setTooltipContent(displayLabel);
        } else {
            markers[name] = L.marker([pos.lat, pos.lon], { icon: iconColor(markerColor) })
                .bindTooltip(displayLabel, { permanent: true, direction: 'top', offset: [0, -10] })
                .addTo(map);
        }
    }

    // detail card
    let detail = '';
    if (s) {
        const dataEntries = s.data || {};
        const lines = [];
        for (const [key, val] of Object.entries(dataEntries)) {
            if (!val) {
                lines.push(`${key}: N/A`);
            } else if (key === 'gps') {
                lines.push(`${key}: lat=${val.latitude??'?'} lon=${val.longitude??'?'} alt=${val.altitude??'?'} spd=${val.speed??'?'} sats=${val.num_satellites??'?'}`);
            } else {
                // Generic: show timestamp + compact JSON
                const ts = findTimestamp(val);
                const tsStr = ts ? ts : '?';
                const rest = Object.fromEntries(Object.entries(val).filter(([k]) => !TS_FIELDS.includes(k)));
                lines.push(`${key}: ts=${tsStr} ${JSON.stringify(rest)}`);
            }
        }
        detail = lines.join('\n');
    } else {
        detail = `Error: ${err || 'unknown'}`;
    }

    return `<div class="card ${cls}"><h3>${displayLabel}${badges}</h3><pre>${detail}</pre></div>`;
}

async function poll() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();
        let html = '';
        for (const [name, info] of Object.entries(data)) {
            html += renderHost(name, info);
        }
        document.getElementById('panel').innerHTML = html || '<p>No targets</p>';
    } catch(e) {
        document.getElementById('panel').innerHTML = `<p style="color:#f33">Fetch error: ${e}</p>`;
    }
}

setInterval(poll, 1000);
poll();
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/status":
            self._serve_api()
        else:
            self.send_error(404)

    def _serve_html(self):
        body = HTML_TEMPLATE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_api(self):
        data = self.server.poller.snapshot()
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # suppress access log


# ===================================================================
# targets file parser
# ===================================================================

def load_targets(path: str) -> list[dict]:
    """
    targets ファイル: 1 行 1 ターゲット
    フォーマット: address:port  (例: 192.168.1.10:20000)
    '#' で始まる行・空行はスキップ
    """
    targets = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # address:port
            if ":" not in line:
                print(f"警告: 不正な行をスキップ: {line}", file=sys.stderr)
                continue
            host, port = line.rsplit(":", 1)
            address = f"{host}:{port}"
            name = host  # 初期名はアドレス。clt 応答の hostname で上書き
            url = f"http://{host}:{port}"
            targets.append({"name": name, "address": address, "url": url})
    return targets


# ===================================================================
# main
# ===================================================================

def main():
    parser = argparse.ArgumentParser(description="explogger-man dashboard")
    parser.add_argument(
        "targets",
        help="targets ファイルパス (1 行 1 ターゲット: address:port)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="ダッシュボードの待ち受けポート (デフォルト: 8080)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.8,
        help="各 clt への GET タイムアウト秒 (デフォルト: 0.8)",
    )
    args = parser.parse_args()

    targets = load_targets(args.targets)
    if not targets:
        print("Error: targets が空です", file=sys.stderr)
        sys.exit(1)

    print(f"explogger-man starting", file=sys.stderr)
    print(f"  dashboard : http://0.0.0.0:{args.port}/", file=sys.stderr)
    print(f"  targets   : {len(targets)}", file=sys.stderr)
    for t in targets:
        print(f"    {t['name']} -> {t['url']}", file=sys.stderr)

    poller = Poller(targets, timeout=args.timeout)
    poller.start()

    server = HTTPServer(("0.0.0.0", args.port), DashboardHandler)
    server.poller = poller

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
    finally:
        poller.stop()
        server.server_close()


if __name__ == "__main__":
    main()
