#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
speed_test.py — 从任意一台机器对 Cloudflare 边缘 IP 做吞吐测速。

两种模式:
  direct  直连边缘 IP 下载(经 --resolve 指定 IP,测量 你 <-> 边缘 的原始吞吐)
  tunnel  走完整 VLESS xHTTP 隧道下载(测量真实用户体验)

用法示例:
  # 指定 UUID,对 3 个 IP 测「直连 + 隧道」
  python3 speed_test.py --uuid <client-uuid> --host node.example.com \
      --path /your-xhttp-path 104.16.0.1 172.64.0.1 104.18.0.1

  # 在 x-ui 源站上,自动从数据库读 UUID
  sudo python3 speed_test.py --from-db --host node.example.com --path /p

  # 不给 IP 则自动解析候选域名,过滤出 Cloudflare IPv4
  python3 speed_test.py --host node.example.com --path /p
"""

import argparse
import ipaddress
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time

CLOUDFLARE_CIDRS = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
    "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
    "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
)
CF_NETWORKS = tuple(ipaddress.ip_network(c) for c in CLOUDFLARE_CIDRS)
DEFAULT_CANDIDATE_DOMAINS = ("openai.com", "cloudflare.com", "www.cloudflare.com", "workers.dev")


def is_cf_ip(v):
    try:
        a = ipaddress.ip_address(v)
    except ValueError:
        return False
    return a.version == 4 and any(a in n for n in CF_NETWORKS)


def discover(ips, domains):
    if ips:
        return [ip for ip in ips if is_cf_ip(ip) or not ipaddress.ip_address(ip).version] or list(ips)
    found = set()
    for d in domains:
        try:
            for row in socket.getaddrinfo(d, 443, socket.AF_INET, socket.SOCK_STREAM):
                if is_cf_ip(row[4][0]):
                    found.add(row[4][0])
        except OSError:
            pass
    return sorted(found)


def client_uuid_from_db(db, inbound_id):
    import sqlite3
    conn = sqlite3.connect(db, timeout=10)
    try:
        settings = json.loads(conn.execute(
            "select settings from inbounds where id=?", (inbound_id,)).fetchone()[0])
    finally:
        conn.close()
    for client in settings.get("clients", []):
        if client.get("enable") and client.get("id"):
            return client["id"]
    raise RuntimeError("no enabled client found in database")


def reserve_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def download_once(args, extra):
    """extra: 直连模式为 --resolve 参数;隧道模式为 --socks5-hostname 参数。"""
    r = subprocess.run(
        ["curl", "-sS", "-L", *extra, "--connect-timeout", "8",
         "--max-time", str(args.max_time), "-o", "/dev/null",
         "-w", "%{http_code} %{speed_download} %{time_total} %{size_download}",
         f"https://speed.cloudflare.com/__down?bytes={args.bytes}"],
        capture_output=True, text=True, timeout=args.max_time + 8)
    f = r.stdout.strip().split()
    if r.returncode in (0, 28) and len(f) == 4 and f[0] == "200" and int(f[3]) > args.bytes * 0.4:
        return float(f[1]), float(f[2])
    return None


def direct_speed(ip, args, rounds):
    speeds, times = [], []
    for _ in range(rounds):
        got = download_once(args, ["--resolve", f"speed.cloudflare.com:443:{ip}"])
        if got:
            speeds.append(got[0])
            times.append(got[1])
    return (statistics.median(speeds), statistics.median(times)) if speeds else None


def tunnel_speed(ip, args, rounds, uuid):
    speeds, times = [], []
    for _ in range(rounds):
        port = reserve_port()
        cfg = {
            "log": {"loglevel": "error"},
            "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "socks",
                          "settings": {"udp": False}}],
            "outbounds": [{"protocol": "vless",
                           "settings": {"vnext": [{"address": ip, "port": 443,
                                                   "users": [{"id": uuid, "encryption": "none"}]}]},
                          "streamSettings": {"network": "xhttp", "security": "tls",
                                             "tlsSettings": {"serverName": args.host},
                                             "xhttpSettings": {"path": args.path, "host": args.host,
                                                               "mode": "auto",
                                                               "xPaddingBytes": "100-1000"}}}],
        }
        fd, cp = tempfile.mkstemp(prefix="spdtest-", suffix=".json")
        os.close(fd)
        with open(cp, "w") as f:
            json.dump(cfg, f)
        proc = subprocess.Popen([args.xray, "run", "-c", cp],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(0.8)
            got = download_once(args, ["--socks5-hostname", f"127.0.0.1:{port}"])
            if got:
                speeds.append(got[0])
                times.append(got[1])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
            os.unlink(cp)
    return (statistics.median(speeds), statistics.median(times)) if speeds else None


def main():
    ap = argparse.ArgumentParser(description="Cloudflare edge IP throughput test")
    ap.add_argument("ips", nargs="*", help="candidate edge IPs (default: resolve candidate domains)")
    ap.add_argument("--host", required=True, help="SNI / HTTP Host of the proxied service")
    ap.add_argument("--path", required=True, help="xHTTP path")
    ap.add_argument("--uuid", help="VLESS client UUID")
    ap.add_argument("--from-db", action="store_true",
                    help="read UUID from x-ui database (run on the x-ui server)")
    ap.add_argument("--db", default="/etc/x-ui/x-ui.db")
    ap.add_argument("--inbound-id", type=int, default=1)
    ap.add_argument("--xray", default="/usr/local/x-ui/bin/xray-linux-amd64")
    ap.add_argument("--bytes", type=int, default=9_000_000,
                    help="bytes per download (speed.cloudflare.com caps around 10MB)")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--max-time", type=int, default=25)
    ap.add_argument("--mode", choices=["direct", "tunnel", "both"], default="both")
    ap.add_argument("--domains", nargs="*", default=list(DEFAULT_CANDIDATE_DOMAINS))
    args = ap.parse_args()

    if args.mode in ("tunnel", "both"):
        if args.from_db:
            args.uuid = client_uuid_from_db(args.db, args.inbound_id)
        if not args.uuid:
            ap.error("--uuid or --from-db is required for tunnel mode")
        if not os.path.exists(args.xray):
            ap.error(f"xray binary not found: {args.xray}")

    targets = discover(args.ips, args.domains)
    if not targets:
        print("no Cloudflare edge IPs found", file=sys.stderr)
        return 1

    print(f"{'IP':<16} {'mode':<8} {'MB/s':>7} {'Mbps':>7} {'time_s':>7}")
    for ip in targets:
        rows = []
        if args.mode in ("direct", "both"):
            rows.append(("direct", direct_speed(ip, args, args.rounds)))
        if args.mode in ("tunnel", "both"):
            rows.append(("tunnel", tunnel_speed(ip, args, args.rounds, args.uuid)))
        for label, got in rows:
            if got:
                print(f"{ip:<16} {label:<8} {got[0]/1e6:>7.2f} {got[0]*8/1e6:>7.1f} {got[1]:>7.2f}")
            else:
                print(f"{ip:<16} {label:<8} {'FAIL':>7} {'':>7} {'':>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
