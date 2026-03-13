#!/usr/bin/env python3
"""
explogger-clt - ex-logger client status API server

data/ 以下の各ロガーの最新データを /status で返す Web API サーバ。
"""

import argparse
import json
import math
import os
import random
import socket
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path


class StatusHandler(BaseHTTPRequestHandler):
    """GET /status に応答するハンドラ"""

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        else:
            self.send_error(404)

    def _handle_status(self):
        srv = self.server
        data_dir = srv.data_dir

        status = {
            "time": datetime.now(timezone.utc).isoformat(),
            "hostname": srv.clt_hostname,
            "data": {},
        }

        # data/ 以下の各サブディレクトリをデータソースとして走査
        if data_dir.is_dir():
            for sub in sorted(data_dir.iterdir()):
                if not sub.is_dir():
                    continue
                key = sub.name  # フォルダ名 = データ名
                # GPS の pos ファイルがあればそちらを優先
                files = list(sub.glob("*_pos.log"))
                if not files:
                    files = list(sub.glob("*"))
                    files = [f for f in files if f.is_file()]
                status["data"][key] = self._latest_from_files(files)

        body = json.dumps(status, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # --- util ----------------------------------------------------------
    @classmethod
    def _latest_from_files(cls, files: list[Path]) -> dict | None:
        """mtime が新しいファイルから順に試し、最初にデータが取れたものを返す。
        サービス再起動直後に最新ファイルが空でも1つ前から読める。"""
        by_mtime = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)
        for path in by_mtime:
            result = cls._read_last_json_line(path)
            if result is not None:
                return result
        return None

    # --- util ----------------------------------------------------------
    @staticmethod
    def _read_last_json_line(path: Path) -> dict | None:
        """ファイル末尾から最後の非空行を JSON パースして返す"""
        try:
            with open(path, "rb") as f:
                # 末尾から最大 4 KB だけ読む
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4096))
                tail = f.read().decode(errors="replace")
            lines = [l for l in tail.splitlines() if l.strip()]
            if not lines:
                return None
            return json.loads(lines[-1])
        except Exception:
            return None

    def log_message(self, format, *args):
        # アクセスログを stderr に出す
        sys.stderr.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"{self.client_address[0]} - {format % args}\n"
        )


# ===================================================================
# Demo mode
# ===================================================================

class DemoDataGenerator:
    """指定した中心点の周囲 ~100m を右往左往するデモデータを生成"""

    # 緯度1度 ≒ 111320m, 経度1度 ≒ 111320m * cos(lat)
    METER_PER_DEG_LAT = 111320.0

    def __init__(self, center_lat: float = 35.681236, center_lon: float = 139.767125):
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.meter_per_deg_lon = self.METER_PER_DEG_LAT * math.cos(math.radians(center_lat))
        self._t0 = time.monotonic()
        # random walk state (meters from center)
        self._x = 0.0
        self._y = 0.0
        self._vx = random.uniform(-2, 2)
        self._vy = random.uniform(-2, 2)

    def _step(self):
        """1 ステップ分のランダムウォーク (呼び出しごとに位置更新)"""
        # 加速度にランダム揺らぎ
        self._vx += random.gauss(0, 0.5)
        self._vy += random.gauss(0, 0.5)
        # 速度制限 (~3 m/s)
        speed = math.hypot(self._vx, self._vy)
        if speed > 3.0:
            self._vx *= 3.0 / speed
            self._vy *= 3.0 / speed
        self._x += self._vx
        self._y += self._vy
        # 中心から 100m 以上離れたら引き戻す
        dist = math.hypot(self._x, self._y)
        if dist > 100.0:
            self._vx -= self._x * 0.05
            self._vy -= self._y * 0.05

    def gps(self) -> dict:
        self._step()
        now = datetime.now(timezone.utc)
        lat = self.center_lat + self._y / self.METER_PER_DEG_LAT
        lon = self.center_lon + self._x / self.meter_per_deg_lon
        speed = math.hypot(self._vx, self._vy)
        track = math.degrees(math.atan2(self._vx, self._vy)) % 360
        return {
            "type": "GPSD_TPV",
            "system_timestamp": now.isoformat(timespec="milliseconds"),
            "time": now.isoformat(timespec="milliseconds"),
            "mode": 3,
            "latitude": round(lat, 8),
            "longitude": round(lon, 8),
            "altitude": round(30.0 + random.gauss(0, 0.5), 1),
            "speed": round(speed, 2),
            "track": round(track, 1),
            "num_satellites": random.randint(8, 14),
            "num_satellites_used": random.randint(6, 12),
        }

    def netmon(self) -> dict:
        now = datetime.now(timezone.utc)
        rx_b = random.randint(500, 50000)
        tx_b = random.randint(500, 50000)
        rx_p = random.randint(5, 200)
        tx_p = random.randint(5, 200)
        return {
            "timestamp": now.isoformat(),
            "elapsed_sec": round(1.0 + random.gauss(0, 0.001), 6),
            "interfaces": {
                "eth0": {
                    "rx_bytes": rx_b,
                    "rx_packets": rx_p,
                    "tx_bytes": tx_b,
                    "tx_packets": tx_p,
                    "rx_bytes_rate": round(rx_b / 1.0, 2),
                    "rx_packets_rate": round(rx_p / 1.0, 2),
                    "tx_bytes_rate": round(tx_b / 1.0, 2),
                    "tx_packets_rate": round(tx_p / 1.0, 2),
                }
            },
        }


class DemoStatusHandler(BaseHTTPRequestHandler):
    """デモモード用ハンドラ: ファイルを読まず生成データを返す"""

    def do_GET(self):
        if self.path == "/status":
            self._handle_status()
        else:
            self.send_error(404)

    def _handle_status(self):
        srv = self.server
        demo = srv.demo_generator
        status = {
            "time": datetime.now(timezone.utc).isoformat(),
            "hostname": srv.clt_hostname,
            "data": {
                "gps": demo.gps(),
                "netmon": demo.netmon(),
            },
        }
        body = json.dumps(status, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        sys.stderr.write(
            f"[{datetime.now().isoformat(timespec='seconds')}] "
            f"{self.client_address[0]} - {format % args}\n"
        )


def main():
    parser = argparse.ArgumentParser(description="explogger-clt status API server")
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="設定ファイルパス (JSON)",
    )
    parser.add_argument(
        "--hostname",
        default=None,
        help="このクライアントのホスト名 (デフォルト: OS ホスト名)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="待ち受けポート (デフォルト: 20000)",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        metavar="DIR",
        help="data ディレクトリのパス (デフォルト: スクリプト隣の data/)",
    )
    parser.add_argument(
        "-D", "--demo",
        action="store_true",
        help="デモモード: 実データの代わりにランダム生成データを返す",
    )
    parser.add_argument(
        "--demo-lat",
        type=float,
        default=35.681236,
        help="デモモードの中心緯度 (デフォルト: 35.681236 東京駅)",
    )
    parser.add_argument(
        "--demo-lon",
        type=float,
        default=139.767125,
        help="デモモードの中心経度 (デフォルト: 139.767125 東京駅)",
    )
    args = parser.parse_args()

    # --- 設定の読み込み (config < CLI 引数で上書き) ---
    cfg = {}
    if args.config:
        with open(args.config) as f:
            cfg = json.load(f)

    clt_hostname = args.hostname or cfg.get("hostname") or socket.gethostname()
    port = args.port or cfg.get("port") or 20000
    default_data_dir = str(Path(__file__).resolve().parent / "data")
    data_dir = Path(args.data_dir or cfg.get("data_dir") or default_data_dir)

    if args.demo:
        print(f"explogger-clt starting (DEMO MODE)", file=sys.stderr)
        print(f"  hostname   : {clt_hostname}", file=sys.stderr)
        print(f"  port       : {port}", file=sys.stderr)
        print(f"  demo center: {args.demo_lat}, {args.demo_lon}", file=sys.stderr)

        server = HTTPServer(("0.0.0.0", port), DemoStatusHandler)
        server.clt_hostname = clt_hostname
        server.demo_generator = DemoDataGenerator(
            center_lat=args.demo_lat, center_lon=args.demo_lon,
        )
    else:
        print(f"explogger-clt starting", file=sys.stderr)
        print(f"  hostname : {clt_hostname}", file=sys.stderr)
        print(f"  port     : {port}", file=sys.stderr)
        print(f"  data_dir : {data_dir}", file=sys.stderr)

        server = HTTPServer(("0.0.0.0", port), StatusHandler)
        server.clt_hostname = clt_hostname
        server.data_dir = data_dir

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
