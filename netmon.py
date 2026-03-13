#!/usr/bin/env python3
"""
netmon.py - Network interface In/Out pkts/bytes monitor at 1Hz
Usage: python3 netmon.py --interfaces eth0 eth1 --output /path/to/dir
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


PROC_NET_DEV = "/proc/net/dev"


def read_iface_stats(interfaces: list[str]) -> dict:
    """
    /proc/net/dev から指定インタフェースの統計を読む
    返り値: {iface: {rx_bytes, rx_packets, tx_bytes, tx_packets}}
    """
    stats = {}
    with open(PROC_NET_DEV) as f:
        for line in f:
            line = line.strip()
            if ":" not in line:
                continue
            iface, data = line.split(":", 1)
            iface = iface.strip()
            if iface not in interfaces:
                continue
            fields = data.split()
            # /proc/net/dev カラム順:
            # rx: bytes packets errs drop fifo frame compressed multicast
            # tx: bytes packets errs drop fifo colls carrier compressed
            stats[iface] = {
                "rx_bytes":   int(fields[0]),
                "rx_packets": int(fields[1]),
                "tx_bytes":   int(fields[8]),
                "tx_packets": int(fields[9]),
            }
    return stats


def diff_stats(prev: dict, curr: dict) -> dict:
    """2時点間の差分を計算（カウンタラップ未考慮）"""
    result = {}
    for iface in curr:
        if iface not in prev:
            continue
        result[iface] = {
            key: curr[iface][key] - prev[iface][key]
            for key in curr[iface]
        }
    return result


def open_logfile(output_dir: Path) -> tuple:
    """YYYYMMDD_HHMMSS.log を開く"""
    now = datetime.now()
    filename = now.strftime("%Y%m%d_%H%M%S") + ".log"
    path = output_dir / filename
    return open(path, "w", buffering=1), path  # line buffered


def main():
    parser = argparse.ArgumentParser(description="Network interface monitor (1Hz, JSONL)")
    parser.add_argument(
        "--interfaces", "-i",
        nargs="+",
        required=True,
        metavar="IFACE",
        help="監視するインタフェース名 (例: eth0 eth1)",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        metavar="DIR",
        help="ログ出力ディレクトリ",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        metavar="SEC",
        help="サンプリング間隔(秒) デフォルト: 1.0",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # /proc/net/dev に存在するか確認
    available = read_iface_stats(args.interfaces)
    missing = [i for i in args.interfaces if i not in available]
    if missing:
        print(f"[WARN] インタフェースが見つかりません: {missing}", file=sys.stderr)

    print(f"[INFO] 監視対象: {args.interfaces}", file=sys.stderr)
    print(f"[INFO] 出力先: {output_dir}", file=sys.stderr)

    logfile, logpath = open_logfile(output_dir)
    print(f"[INFO] ログファイル: {logpath}", file=sys.stderr)

    prev_stats = read_iface_stats(args.interfaces)
    prev_time = time.monotonic()

    # 積算カウンタ (netmon 起動時点からの累計)
    cumulative: dict[str, dict[str, int]] = {}
    for iface in args.interfaces:
        cumulative[iface] = {
            "rx_bytes": 0, "rx_packets": 0,
            "tx_bytes": 0, "tx_packets": 0,
        }

    # ログローテーション用（日付変わりで新ファイル）
    current_date = datetime.now().date()

    try:
        while True:
            time.sleep(args.interval)

            now = datetime.now(timezone.utc)
            now_monotonic = time.monotonic()
            curr_stats = read_iface_stats(args.interfaces)
            elapsed = now_monotonic - prev_time

            delta = diff_stats(prev_stats, curr_stats)

            record = {
                "timestamp": now.isoformat(),
                "elapsed_sec": round(elapsed, 6),
                "interfaces": {},
            }

            for iface in args.interfaces:
                if iface in delta:
                    d = delta[iface]
                    # 積算カウンタを更新
                    for key in cumulative[iface]:
                        cumulative[iface][key] += d[key]
                    record["interfaces"][iface] = {
                        "rx_bytes":        d["rx_bytes"],
                        "rx_packets":      d["rx_packets"],
                        "tx_bytes":        d["tx_bytes"],
                        "tx_packets":      d["tx_packets"],
                        "rx_bytes_rate":   round(d["rx_bytes"]   / elapsed, 2),
                        "rx_packets_rate": round(d["rx_packets"] / elapsed, 2),
                        "tx_bytes_rate":   round(d["tx_bytes"]   / elapsed, 2),
                        "tx_packets_rate": round(d["tx_packets"] / elapsed, 2),
                        "total_rx_bytes":   cumulative[iface]["rx_bytes"],
                        "total_rx_packets": cumulative[iface]["rx_packets"],
                        "total_tx_bytes":   cumulative[iface]["tx_bytes"],
                        "total_tx_packets": cumulative[iface]["tx_packets"],
                    }
                else:
                    record["interfaces"][iface] = None  # 読み取り失敗

            logfile.write(json.dumps(record) + "\n")

            prev_stats = curr_stats
            prev_time = now_monotonic

            # 日付が変わったらログローテーション
            today = datetime.now().date()
            if today != current_date:
                logfile.close()
                logfile, logpath = open_logfile(output_dir)
                print(f"[INFO] ログローテーション: {logpath}", file=sys.stderr)
                current_date = today

    except KeyboardInterrupt:
        print("[INFO] 終了", file=sys.stderr)
    finally:
        logfile.close()


if __name__ == "__main__":
    main()
