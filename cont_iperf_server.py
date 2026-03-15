#!/usr/bin/env python3
"""
cont_iperf_server.py - iperf3 サーバを連続起動し、計測周期ごとの結果を JSONL で記録
Usage: python3 cont_iperf_server.py --port 5201 --output /path/to/dir
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from logging import getLogger, Formatter, StreamHandler, DEBUG

# Set up logger
logger = getLogger(__name__)
logger.setLevel(DEBUG)
handler = StreamHandler()
handler.setLevel(DEBUG)
formatter = Formatter('%(asctime)s\t%(levelname)s\t%(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Global state
iperf_process = None


def signal_handler(_signum, _frame):
    global iperf_process
    logger.info('Received interrupt signal, stopping...')
    if iperf_process:
        iperf_process.send_signal(signal.SIGTERM)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


# ===================================================================
# iperf3 テキスト出力パーサ
# ===================================================================

# 単位変換テーブル
UNIT_MULTIPLIER_BYTES = {
    "bytes":  1,
    "kbytes": 1024,
    "mbytes": 1024**2,
    "gbytes": 1024**3,
}

UNIT_MULTIPLIER_BITS = {
    "bits/sec":  1,
    "kbits/sec": 1e3,
    "mbits/sec": 1e6,
    "gbits/sec": 1e9,
}

# インターバル行のパターン
# [  5]   0.00-1.00   sec  1.10 MBytes  9.22 Mbits/sec
# [SUM]   0.00-1.00   sec  2.20 MBytes  18.4 Mbits/sec
# --timestamps 付きの場合、先頭にタイムスタンプ文字列が付く
RE_INTERVAL = re.compile(
    r'\[\s*(?P<stream_id>\d+|SUM)\]'
    r'\s+(?P<start>[\d.]+)-(?P<end>[\d.]+)\s+sec'
    r'\s+(?P<transfer>[\d.]+)\s+(?P<transfer_unit>\w+)'
    r'\s+(?P<bitrate>[\d.]+)\s+(?P<bitrate_unit>\S+/sec)'
)

# "Accepted connection" 行
RE_ACCEPTED = re.compile(
    r'Accepted connection from (?P<client_addr>\S+), port (?P<client_port>\d+)'
)

# サマリ行: "sender" / "receiver" が末尾に付く
RE_SUMMARY = re.compile(r'\b(sender|receiver)\s*$')


def parse_interval_line(line: str) -> dict | None:
    """iperf3 のインターバル出力行をパースして dict を返す。非該当なら None。"""
    m = RE_INTERVAL.search(line)
    if not m:
        return None

    # サマリ行 (sender/receiver) はスキップ
    if RE_SUMMARY.search(line):
        return None

    transfer_val = float(m.group("transfer"))
    transfer_unit = m.group("transfer_unit").lower()
    bitrate_val = float(m.group("bitrate"))
    bitrate_unit = m.group("bitrate_unit").lower()

    transfer_bytes = transfer_val * UNIT_MULTIPLIER_BYTES.get(transfer_unit, 1)
    bitrate_bps = bitrate_val * UNIT_MULTIPLIER_BITS.get(bitrate_unit, 1)

    return {
        "stream_id": m.group("stream_id"),
        "interval_start": float(m.group("start")),
        "interval_end": float(m.group("end")),
        "transfer_bytes": round(transfer_bytes),
        "bitrate_bps": round(bitrate_bps, 2),
    }


def parse_accepted_line(line: str) -> dict | None:
    """Accepted connection 行をパースして dict を返す。"""
    m = RE_ACCEPTED.search(line)
    if not m:
        return None
    return {
        "client_addr": m.group("client_addr"),
        "client_port": int(m.group("client_port")),
    }


def parse_summary_line(line: str) -> dict | None:
    """サマリ行 (sender/receiver) をパースして dict を返す。"""
    m_sum = RE_SUMMARY.search(line)
    if not m_sum:
        return None
    m = RE_INTERVAL.search(line)
    if not m:
        return None

    transfer_val = float(m.group("transfer"))
    transfer_unit = m.group("transfer_unit").lower()
    bitrate_val = float(m.group("bitrate"))
    bitrate_unit = m.group("bitrate_unit").lower()

    transfer_bytes = transfer_val * UNIT_MULTIPLIER_BYTES.get(transfer_unit, 1)
    bitrate_bps = bitrate_val * UNIT_MULTIPLIER_BITS.get(bitrate_unit, 1)

    return {
        "stream_id": m.group("stream_id"),
        "interval_start": float(m.group("start")),
        "interval_end": float(m.group("end")),
        "transfer_bytes": round(transfer_bytes),
        "bitrate_bps": round(bitrate_bps, 2),
        "direction": m_sum.group(1),  # "sender" or "receiver"
    }


# ===================================================================
# メイン処理
# ===================================================================

def open_logfile(output_dir: Path):
    """YYYYMMDD_HHMMSS.log を開く"""
    now = datetime.now()
    filename = now.strftime("%Y%m%d_%H%M%S") + ".log"
    path = output_dir / filename
    return open(path, "w", buffering=1), path  # line buffered


def run_server(port, output_dir, interval):
    global iperf_process

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    logfile, logpath = open_logfile(output_dir)
    logger.info(f'Log file: {logpath}')

    # ログローテーション用（日付変わりで新ファイル）
    current_date = datetime.now().date()

    # 現在のセッション情報
    current_client = None

    while True:
        command = [
            'iperf3',
            '-s',
            '-p', str(port),
            '--one-off',
            '--forceflush',
            '--timestamps',
            '--rcv-timeout', '5000',
            '-i', str(interval),
        ]

        logger.info(f'Waiting for client on port {port}...')

        iperf_process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=1, universal_newlines=True
        )

        has_output = False
        current_client = None

        for line in iperf_process.stdout:
            line = line.rstrip('\n')
            if not line.strip():
                continue

            has_output = True

            # Accepted connection
            accepted = parse_accepted_line(line)
            if accepted:
                current_client = accepted
                logger.info(f'Client connected: {accepted["client_addr"]}:{accepted["client_port"]}')
                continue

            # サマリ行 (sender/receiver)
            summary = parse_summary_line(line)
            if summary:
                now = datetime.now(timezone.utc)
                record = {
                    "timestamp": now.isoformat(),
                    "type": "summary",
                    "port": port,
                }
                if current_client:
                    record["client_addr"] = current_client["client_addr"]
                    record["client_port"] = current_client["client_port"]
                record.update(summary)
                logfile.write(json.dumps(record, ensure_ascii=False) + "\n")
                logfile.flush()
                logger.info(
                    f'Summary ({summary["direction"]}): '
                    f'{summary["transfer_bytes"]} bytes, '
                    f'{summary["bitrate_bps"] / 1e6:.2f} Mbps'
                )
                continue

            # インターバル行
            parsed = parse_interval_line(line)
            if parsed:
                now = datetime.now(timezone.utc)
                record = {
                    "timestamp": now.isoformat(),
                    "type": "interval",
                    "port": port,
                }
                if current_client:
                    record["client_addr"] = current_client["client_addr"]
                    record["client_port"] = current_client["client_port"]
                record.update(parsed)
                logfile.write(json.dumps(record, ensure_ascii=False) + "\n")
                logfile.flush()
                continue

        iperf_process.wait()
        returncode = iperf_process.returncode
        iperf_process = None

        if returncode is not None and returncode < 0:
            logger.info(f'Server stopped by signal (returncode={returncode}).')
            break

        if not has_output:
            logger.info('No data received (server interrupted before client connected).')
            break

        # 日付が変わったらログローテーション
        today = datetime.now().date()
        if today != current_date:
            logfile.close()
            logfile, logpath = open_logfile(output_dir)
            logger.info(f'Log rotation: {logpath}')
            current_date = today

        current_client = None
        logger.info('Session complete, waiting for next client...')

    logfile.close()


def main():
    parser = argparse.ArgumentParser(description='iperf3 server with JSONL logging')
    parser.add_argument('--port', type=int, default=5201,
                        help='iperf3 server port (default: 5201)')
    parser.add_argument('--output', '-o', required=True,
                        help='Output directory for JSONL logs')
    parser.add_argument('--interval', '-i', type=float, default=1.0,
                        help='iperf3 reporting interval in seconds (default: 1.0)')
    args = parser.parse_args()

    logger.info(f'Starting iperf3 server on port {args.port}, logging to {args.output}')

    run_server(args.port, args.output, args.interval)

    logger.info('Done.')


if __name__ == '__main__':
    main()
