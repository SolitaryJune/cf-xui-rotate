# cf-xui-rotate — Cloudflare 优选 IP 自动轮换(x-ui / Xray)

让 [x-ui](https://github.com/MHSanaei/3x-ui) 管理的 VLESS + xHTTP 节点自动吃上 Cloudflare 优选:
**客户端连接地址固定为一个域名,该域名由后台定时探测并自动指向当前最快的 Cloudflare 边缘 IP**。

- 客户端/订阅链接**永久不变**,IP 轮换对用户完全透明
- 探测不是 TCP ping,而是**真实 VLESS xHTTP 全链路探测**(起临时 Xray 实例走完整隧道)
- 轮换只改一条灰云 DNS 记录(Cloudflare API),**不碰 x-ui 数据库、不重启任何服务**
- 带防抖动(当前 IP 仍在最优 1.2 倍内则保持)、文件锁、失败回滚、数据库自动备份

## 架构

```text
═══════════════ 数据面:用户流量 ═══════════════

  客户端 (VLESS xHTTP)
      │  连接地址 = 优选域名(OPT_DOMAIN,灰云)
      │  TLS SNI / HTTP Host = 业务域名(TARGET_HOST,小黄云)
      ▼
  Cloudflare 边缘(当前最优 IP,定时轮换)
      │  按 SNI 匹配 zone,回源
      ▼
  源站服务器 Nginx :443
      │  location 路径 → 127.0.0.1:<port>
      ▼
  Xray 入站(VLESS + xHTTP,仅监听回环)
      ▼
  目标网站

═══════════════ 订阅分发 ═══════════════

  客户端 ──> 订阅地址(x-ui 订阅或反代)
                 └─ x-ui hosts 表:地址=优选域名(固定)
                    生成的节点自动跟随 DNS 轮换

═══════════════ 控制面:定时轮换 ═══════════════

  systemd timer(默认 6h + 随机延迟)
      └─> cf-xui-rotate.py
            ├─ 1. 解析候选域名 → 过滤出 Cloudflare 官方 IPv4 段
            ├─ 2. 逐个发起真实 xHTTP 探测(临时 Xray + curl)
            ├─ 3. 选最快;当前 IP 在 1.2× 内则保持(防抖动)
            ├─ 4. 调 Cloudflare API 更新灰云 A 记录(TTL 120s)
            └─ 5. 状态落盘 + 日志;DNS 失败只报错不动线上
```

## 与常见做法对比

| 方案 | 问题 |
|---|---|
| 手动测速 + 改客户端地址 | 纯手工,IP 失效就得改 |
| 订阅里写死某个优选 IP | IP 是快照,过期后体验劣化且无感知 |
| `/etc/hosts` 覆盖 | 只对这一台机器生效,别的用户享受不到 |
| **本方案(优选域名)** | **域名不变、IP 自动跟手,所有用户同时受益** |

## 快速开始

### 前提

- 一台源站服务器(如 VPS),装有 x-ui(3.x)和 Nginx,Xray 入站为 VLESS + xHTTP 且监听回环;
- 一个 Cloudflare 托管的域名(zone);
- 两个子域名:
  - `node.example.com` — **业务域名**,开启小黄云(Proxied),回源到源站,Nginx 上配好 TLS 和 xHTTP 路径;
  - `best.example.com` — **优选域名**,灰云(DNS only)A 记录,初始值随意,之后由脚本自动更新;
- 一个 Cloudflare API Token,权限仅需 `Zone → DNS → Edit`(最小权限)。

### 安装

```bash
# 1. 配置(不要把 token 写进代码或仓库)
sudo install -m 600 /dev/null /etc/cf-xui-rotate.env
sudo vi /etc/cf-xui-rotate.env          # 参考 examples/cf-xui-rotate.env.example

# 2. 安装脚本与 systemd 单元
sudo install -o root -g root -m 0750 cf-xui-rotate.py /usr/local/bin/cf-xui-rotate
sudo cp systemd/cf-xui-rotate.service systemd/cf-xui-rotate.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cf-xui-rotate.timer

# 3. 手动跑一次并观察
sudo systemctl start cf-xui-rotate.service
sudo tail -f /var/log/cf-xui-rotate.log
```

### 验证

- 日志出现 `changed x.x.x.x -> y.y.y.y ... dns=updated` 或 `kept ... reason=keep-current`;
- `dig +short best.example.com` 返回当前最优 IP;
- x-ui 订阅里节点的 `address` 为优选域名、端口 443、`security=tls`、SNI/Host 为业务域名;
- 客户端正常连接即完成。

## 配置项

环境变量优先于环境文件(默认 `/etc/cf-xui-rotate.env`,可用 `CF_ENV_FILE` 改路径):

| 变量 | 必填 | 说明 |
|---|---|---|
| `CF_API_TOKEN` | 是 | Cloudflare API Token,仅需 `Zone.DNS Edit` |
| `CF_ZONE_ID` | 是 | 域名所在 zone 的 ID |
| `OPT_DOMAIN` | 是 | 优选域名(灰云),客户端连接地址 |
| `TARGET_HOST` | 是 | 业务域名(小黄云),作 SNI + HTTP Host |
| `TEST_PATH` | 是 | xHTTP 路径,与 Nginx/Xray 一致 |
| `HOSTS_REMARK` | 否 | x-ui hosts 表托管条目的备注,默认 `CF优选` |
| `INBOUND_ID` | 否 | x-ui 入站 ID,默认 `1` |
| `CANDIDATE_DOMAINS` | 否 | 候选域名,逗号分隔 |
| `DNS_TTL` | 否 | DNS 记录 TTL,默认 120 秒 |
| `PROBE_ROUNDS` | 否 | 每个候选的探测轮数(取中位数),默认 2 |
| `TEST_URL` | 否 | 探测目标 URL,默认 cloudflare trace |

## 测速工具

`tools/speed_test.py` 用于从任意机器对边缘 IP 做吞吐测速(直连 vs 完整隧道):

```bash
# 在 x-ui 源站上:自动读 UUID,对候选 IP 测直连 + 隧道
sudo python3 tools/speed_test.py --from-db --host node.example.com --path /your-xhttp-path

# 在别的机器上:指定 UUID 和 IP 列表
python3 tools/speed_test.py --uuid <uuid> --host node.example.com --path /your-xhttp-path \
    104.16.0.1 172.64.0.1
```

> 注意:探测下载用的是 `speed.cloudflare.com/__down`,单次大小上限约 10MB。

## 已知局限

- **服务器侧探测 ≠ 用户侧体验**:轮换决策基于源站视角的探测结果,和用户所在运营商的真实带宽可能不一致(尤其是晚高峰)。工具脚本可用于在用户侧机器上做补充测量;
- 优选域名是灰云 A 记录,TTL 120s,轮换后客户端在下一次建连时才会用到新 IP;
- 单一入站(inbound)场景最简单;多入站需要按 inbound 扩展 hosts 逻辑。

## 内核优化建议

源站建议开启 BBR + fq(主流发行版内核自带):

```bash
printf 'net.core.default_qdisc = fq\nnet.ipv4.tcp_congestion_control = bbr\n' \
    | sudo tee /etc/sysctl.d/99-network-tuning.conf
printf 'tcp_bbr\n' | sudo tee /etc/modules-load.d/tcp_bbr.conf
sudo sysctl --system
# 注意:default_qdisc 只对之后注册的网卡生效,存量网卡可手动应用:
# sudo tc qdisc replace dev <iface> root fq
```

## 安全与免责

- API Token、路径、UUID 等敏感信息只放在服务器本地的 env 文件(600 权限),**不要**提交到任何仓库;
- 公开分享节点链接会同时暴露你的业务域名和路径,请自行评估;
- 将 Cloudflare 用于代理流量可能与其服务条款存在冲突,请自行了解并承担风险;
- 本项目仅供个人自建与学习研究使用,请遵守所在地区法律法规,勿用于违法用途。

## License

[MIT](LICENSE)
