#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cf-xui-rotate — Cloudflare 优选 IP 自动轮换(x-ui / Xray 节点)

原理:
  1. 解析一组候选域名,过滤出 Cloudflare 官方 CIDR 内的边缘 IP;
  2. 从本机(源站侧)对每个候选发起「真实 VLESS xHTTP 全链路探测」;
  3. 选最快者:当前 IP 仍在最优 1.2 倍以内则保持不动;
  4. 通过 Cloudflare API 更新一条 DNS-only(灰云)A 记录——
     客户端订阅里的连接地址是这条记录的域名,而不是裸 IP;
  5. x-ui hosts 表只存域名,轮换完全不碰数据库、无需重启任何服务。

配置来源:环境变量 > 环境文件(默认 /etc/cf-xui-rotate.env)> 内置默认值。
所有敏感/个性化信息都应放在环境文件里(chmod 600),不要写进代码。
"""

import fcntl
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import statistics
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---- 固定路径(x-ui 标准布局) ----
DB = Path("/etc/x-ui/x-ui.db")
STATE = Path("/var/lib/cf-xui-rotate.json")
LOCK = Path("/run/lock/cf-xui-rotate.lock")
LOG = Path("/var/log/cf-xui-rotate.log")
XRAY = "/usr/local/x-ui/bin/xray-linux-amd64"

# ---- 可配置项(环境变量 > 环境文件) ----
CF_ENV = Path(os.environ.get("CF_ENV_FILE", "/etc/cf-xui-rotate.env"))

_CFG_DEFAULTS = {
    "CF_API_TOKEN": "",
    "CF_ZONE_ID": "",
    "OPT_DOMAIN": "",           # 灰云优选域名,如 best.example.com(必填)
    "TARGET_HOST": "",          # 小黄云业务域名,作 SNI + HTTP Host(必填)
    "TEST_PATH": "/xhttp",      # xHTTP 路径
    "TEST_URL": "https://www.cloudflare.com/cdn-cgi/trace",
    "INBOUND_ID": "1",
    "HOSTS_REMARK": "CF优选",    # x-ui hosts 表中由本脚本托管的条目备注
    "CANDIDATE_DOMAINS": "openai.com,cloudflare.com,www.cloudflare.com,workers.dev",
    "DNS_TTL": "120",
    "PROBE_ROUNDS": "2",
}


def load_config():
    cfg = dict(_CFG_DEFAULTS)
    try:
        for line in CF_ENV.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key in cfg and value:
                cfg[key] = value
    except OSError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if k in cfg and v})
    return cfg


CFG = load_config()
TARGET_HOST = CFG["TARGET_HOST"]
TEST_PATH = CFG["TEST_PATH"]
TEST_URL = CFG["TEST_URL"]
INBOUND_ID = int(CFG["INBOUND_ID"] or 1)
HOSTS_REMARK = CFG["HOSTS_REMARK"]
DNS_TTL = int(CFG["DNS_TTL"] or 120)
PROBE_ROUNDS = int(CFG["PROBE_ROUNDS"] or 2)
OPT_DOMAIN = CFG["OPT_DOMAIN"]
CANDIDATE_DOMAINS = tuple(
    d.strip() for d in CFG["CANDIDATE_DOMAINS"].split(",") if d.strip()
)

# Cloudflare 官方 IPv4 段,用于过滤候选域名解析结果
CLOUDFLARE_CIDRS = (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22",
    "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
    "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22",
    "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
)
CF_NETWORKS = tuple(ipaddress.ip_network(c) for c in CLOUDFLARE_CIDRS)

# 换 IP 时通过 Cloudflare API 更新 DNS 记录
CF_API_BASE = "https://api.cloudflare.com/client/v4/zones"


def log(message):
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as stream:
            stream.write(line + "\n")
    except OSError:
        pass


def run(args, timeout=20):
    return subprocess.run(
        args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout
    )


def resolve(domain):
    try:
        rows = socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return set()
    return {row[4][0] for row in rows}


def is_cloudflare_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and any(address in n for n in CF_NETWORKS)


def discover_candidates():
    found = set()
    for domain in CANDIDATE_DOMAINS:
        found.update(ip for ip in resolve(domain) if is_cloudflare_ip(ip))
    return sorted(found)


def cf_api(method, path, payload=None):
    token, zone = CFG["CF_API_TOKEN"], CFG["CF_ZONE_ID"]
    if not token or not zone:
        raise RuntimeError("CF_API_TOKEN / CF_ZONE_ID not configured")
    args = [
        "curl", "-sS", "--max-time", "15", "-X", method,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        f"{CF_API_BASE}/{zone}{path}",
    ]
    if payload is not None:
        args += ["--data", json.dumps(payload)]
    result = run(args, 20)
    if result.returncode != 0:
        raise RuntimeError(f"cf api {method} curl rc={result.returncode}: {result.stderr.strip()[:120]}")
    body = json.loads(result.stdout)
    if not body.get("success"):
        raise RuntimeError(f"cf api {method} failed: {json.dumps(body.get('errors'))[:200]}")
    return body.get("result")


def dns_current_ip():
    for ip in resolve(OPT_DOMAIN):
        if is_cloudflare_ip(ip):
            return ip
    return None


def dns_upsert(ip):
    query = f"/dns_records?type=A&name={OPT_DOMAIN}"
    records = cf_api("GET", query)
    payload = {"type": "A", "name": OPT_DOMAIN, "content": ip, "ttl": DNS_TTL, "proxied": False}
    if records:
        cf_api("PUT", f"/dns_records/{records[0]['id']}", payload)
        return "updated"
    cf_api("POST", "/dns_records", payload)
    return "created"


def get_client_id():
    conn = sqlite3.connect(DB, timeout=10)
    try:
        row = conn.execute("select settings from inbounds where id=?", (INBOUND_ID,)).fetchone()
    finally:
        conn.close()
    if not row:
        raise RuntimeError("inbound not found")
    settings = json.loads(row[0])
    for client in settings.get("clients", []):
        if client.get("enable") and client.get("id"):
            return client["id"]
    raise RuntimeError("no enabled VLESS client")


def reserve_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def xray_config(ip, client_id, socks_port):
    return {
        "log": {"loglevel": "error"},
        "inbounds": [
            {"listen": "127.0.0.1", "port": socks_port,
             "protocol": "socks", "settings": {"udp": False}}
        ],
        "outbounds": [
            {"protocol": "vless",
             "settings": {"vnext": [{"address": ip, "port": 443,
                                     "users": [{"id": client_id, "encryption": "none"}]}]},
             "streamSettings": {
                 "network": "xhttp",
                 "security": "tls",
                 "tlsSettings": {"serverName": TARGET_HOST},
                 "xhttpSettings": {"path": TEST_PATH, "host": TARGET_HOST,
                                   "mode": "auto", "xPaddingBytes": "100-1000"},
             }}
        ],
    }


def probe(ip, client_id):
    """对单个边缘 IP 发起一次完整 VLESS xHTTP 探测,返回耗时或 None。"""
    socks_port = reserve_port()
    config_path = None
    process = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", prefix="cf-xui-probe-", suffix=".json", delete=False
        ) as config:
            config_path = config.name
            os.chmod(config_path, 0o600)
            json.dump(xray_config(ip, client_id, socks_port), config)
        process = subprocess.Popen(
            [XRAY, "run", "-c", config_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(0.8)
        if process.poll() is not None:
            return None
        result = run(
            ["curl", "-sS", "-L", "--socks5-hostname", f"127.0.0.1:{socks_port}",
             "--connect-timeout", "8", "--max-time", "18", "-o", "/dev/null",
             "-w", "%{http_code} %{time_total}", TEST_URL],
            22,
        )
        fields = result.stdout.strip().split()
        if result.returncode != 0 or len(fields) != 2 or fields[0] != "200":
            return None
        return float(fields[1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if config_path:
            try:
                os.unlink(config_path)
            except FileNotFoundError:
                pass


def measure(ip, client_id):
    samples = [s for s in (probe(ip, client_id) for _ in range(PROBE_ROUNDS)) if s is not None]
    if not samples:
        return None
    return {"ip": ip, "code": 200, "total": statistics.median(samples), "samples": len(samples)}


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".cf-xui-rotate-", dir=str(STATE.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def prune_backups():
    for path in sorted(DB.parent.glob(DB.name + ".backup-*-rotate"))[:-20]:
        try:
            path.unlink()
        except OSError:
            pass


def ensure_endpoint_domain():
    """保证 x-ui hosts 表的第一条记录指向优选域名(而非某个 IP)。

    正常情况下数据库只在首次部署/人工改动后才需要写;
    IP 的轮换完全通过 DNS 完成,不碰数据库。
    """
    conn = sqlite3.connect(DB, timeout=15)
    try:
        row = conn.execute(
            "select id, address from hosts where inbound_id=? and is_disabled=0 "
            "order by sort_order,id limit 1",
            (INBOUND_ID,),
        ).fetchone()
        if row and row[1] == OPT_DOMAIN:
            return False
    finally:
        conn.close()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = DB.with_name(DB.name + ".backup-" + stamp + "-rotate")
    shutil.copy2(DB, backup)
    conn = sqlite3.connect(DB, timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "select id from hosts where inbound_id=? and remark=? order by sort_order,id limit 1",
            (INBOUND_ID, HOSTS_REMARK),
        ).fetchone()
        if not row:
            row = conn.execute(
                "select id from hosts where inbound_id=? order by sort_order,id limit 1",
                (INBOUND_ID,),
            ).fetchone()
        if row:
            conn.execute(
                "update hosts set address=?, port=443, security='tls', sni=?, "
                "host_header=?, path=?, updated_at=strftime('%s','now')*1000 where id=?",
                (OPT_DOMAIN, TARGET_HOST, TARGET_HOST, TEST_PATH, row[0]),
            )
        else:
            conn.execute(
                "insert into hosts (inbound_id,sort_order,remark,is_disabled,is_hidden,"
                "address,port,security,sni,host_header,path,exclude_from_sub_types) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?)",
                (INBOUND_ID, 0, HOSTS_REMARK, 0, 0, OPT_DOMAIN, 443, "tls",
                 TARGET_HOST, TARGET_HOST, TEST_PATH, ""),
            )
        check = conn.execute("pragma integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError(f"sqlite integrity check failed: {check}")
        conn.commit()
    finally:
        conn.close()
    prune_backups()
    log(f"endpoint address reset to {OPT_DOMAIN} (backup {backup.name})")
    return True


def main():
    if not OPT_DOMAIN or not TARGET_HOST:
        log("missing OPT_DOMAIN / TARGET_HOST in config; nothing to do")
        return 0

    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("skip: another rotation is running")
            return 0

        state = load_state()
        current = dns_current_ip() or state.get("current_ip")
        client_id = get_client_id()
        ensure_endpoint_domain()
        ips = discover_candidates()
        if not ips:
            log("no Cloudflare candidates discovered")
            return 0

        results = [r for r in (measure(ip, client_id) for ip in ips) if r]
        results.sort(key=lambda item: item["total"])
        if not results:
            log("all real xHTTP candidate probes failed; endpoint unchanged")
            return 0

        best = results[0]
        current_result = next((item for item in results if item["ip"] == current), None)
        if current_result and current_result["total"] <= best["total"] * 1.20:
            chosen, reason = current_result, "keep-current"
        else:
            chosen, reason = best, "select-best"

        if chosen["ip"] != current:
            try:
                action = dns_upsert(chosen["ip"])
            except Exception as exc:
                log(f"ERROR dns update to {chosen['ip']} failed: {exc}; "
                    f"keeping {current or OPT_DOMAIN}")
                state["last_run"] = datetime.now(timezone.utc).isoformat()
                state["last_results"] = results[:12]
                state["dns_error"] = str(exc)[:300]
                save_state(state)
                return 0
            state["current_ip"] = chosen["ip"]
            state["changed_at"] = datetime.now(timezone.utc).isoformat()
            state.pop("dns_error", None)
            log(f"changed {current or '-'} -> {chosen['ip']} "
                f"xhttp={chosen['total']:.3f}s reason={reason} dns={action}")
        else:
            log(f"kept {chosen['ip']} xhttp={chosen['total']:.3f}s reason={reason}")

        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state["last_results"] = results[:12]
        save_state(state)
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        log(f"failure {type(exc).__name__}: {exc}")
        raise
