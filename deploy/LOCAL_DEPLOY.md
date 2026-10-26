# 在家用电脑上部署（公网可达）

平台硬要求（`research/06` §2.4 #5）：接口必须 **HTTPS、公网可达**，
`不得指向私有、回环或链路本地地址`。所以「本机能跑」不够，必须有公网入口。

这份文档给三种落地方式，回家按实际网络条件选一种，然后照着「验收清单」逐项打勾。

---

## 零、先量三件事（决定申报并发）

```bash
# 1) 有没有公网 IPv4：两个地址一样就是有（NAT 后面会不同）
curl -s https://ipv4.icanhazip.com
ip route get 1.1.1.1 | awk '{print $7}'      # 或看路由器 WAN 口地址

# 2) 上行带宽（Windows：speedtest 客户端；Linux：装 speedtest-cli）
#    我们要的是 upload，家宽常见 30–100 Mbit，比 2 Mbit 的云主机快十几倍

# 3) 端口是否被封：80/443 在国内家宽基本被封，入站要用高端口
```

带宽和 Search 吞吐的关系（实测数据，真图 150 KB 口径）：

| 出口带宽 | 有效吞吐 | 1,711 条 query 耗时 |
|---|---|---|
| 2 Mbit/s | ≈0.5 req/s | 50 分钟以上 |
| 20 Mbit/s | ≈5 req/s | ≈6 分钟 |
| 50 Mbit/s | ≈12 req/s | ≈2.5 分钟 |
| 100 Mbit/s | ≈16–20 req/s（上游配额封顶） | ≈1.5 分钟 |

---

## 一、方案 A：公网 IP + 路由器端口映射（首选）

适合：上游给了公网 IPv4（含大内网改公网、IPv6 也算）。带宽最好、无中转、免费。

### A1. 端口映射

路由器 → 端口转发 → 内网 IP `192.168.x.x`：

| 外部端口 | 内部 | 说明 |
|---|---|---|
| 8443（举例） | 本机 8443 | 家宽 443 被封，用非标端口 |

本机固定内网 IP（DHCP 绑定 MAC），否则重启后映射失效。

### A2. 域名

- 域名 A 记录指向公网 IP；IP 会变就加 DDNS（路由器自带，或 `ddclient`）。
- **证书用 DNS-01 验证**（不依赖 80/443 入站），Caddy 需要带 DNS 插件的镜像：

```yaml
# docker-compose.override.yml（本地覆盖，不进仓库）
services:
  caddy:
    image: caddy:2-alpine
    # 或自建镜像：FROM caddy:2-builder AS b
    #   RUN xcaddy build --with github.com/caddy-dns/cloudflare
    #   FROM caddy:2-alpine; COPY --from=b /usr/bin/caddy /usr/bin/caddy
    ports: ["8443:8443"]
    environment:
      DOMAIN: "${DOMAIN}"
      CLOUDFLARE_API_TOKEN: "${CLOUDFLARE_API_TOKEN}"
```

```caddyfile
# deploy/Caddyfile.local
{$DOMAIN} {
	encode zstd gzip
	tls {
		dns cloudflare {env.CLOUDFLARE_API_TOKEN}
	}
	reverse_proxy mem-pro:8080 {
		transport http {
			read_timeout 31m
			write_timeout 31m
		}
	}
}
```

没有 DNS 插件就用 `tls internal` + 自签（平台会拒绝自签证书，仅供本地联调）。

### A3. 起服务

```bash
cp service/.env.example .env
# 必填：MEMORY_SYSTEM_KEY=<32 位随机串>、EMBED_MODE=api、DASHSCOPE_API_KEY=...、
#       RERANK_* 按 SUBMISSION.md 里的最终决定
docker compose --profile tls up -d --build
curl -s https://你的域名:8443/health
```

---

## 二、方案 B：内网穿透（没有公网 IP 时）

适合：运营商不给公网 IPv4（CGNAT）、或不想动路由器。

| 产品 | 免费档 | 付费 | 备注 |
|---|---|---|---|
| cpolar / natapp | 随机域名、限速 1–5 Mbit、会断 | 固定域名 + 更高带宽 | 国内延迟低，最省事 |
| Cloudflare Tunnel | 不支持固定域名以外的高级功能 | 免费也能用自定义域名 | 国内访问不稳，评测连跑 1–2 天有风险 |
| frp（自有服务器做 frps） | — | — | 流量仍要过那台机器，2 Mbit 的老问题没解决 |

选它的唯一理由是不折腾网络；**带宽和 SLA 都要按付费档买**。

```bash
docker compose up -d mem-pro                    # 只起服务，TLS 交给穿透
# cpolar / natapp 映射 http://127.0.0.1:8080 → https://xxx.cpolar.cn
```

注意：穿透服务通常自己终止 TLS，`X-Forwarded-For` 和超时要单独确认（平台单请求最长 30 分钟）。

---

## 三、方案 C：云主机 frp 中转（不推荐）

2C2G 那台只当 frps，本机跑服务。省钱但**带宽还是 2 Mbit**，等于没解决，
只有在"必须要固定公网 IP，且暂时不想买带宽"时当过渡。

---

## 四、验收清单（外网环境跑，别在本机 localhost 上自欺）

用**手机热点**、或从另一台机器执行：

```bash
# 1) 契约（27 项，含鉴权/错误码/分页/多模态/幂等）
MEMORY_SYSTEM_KEY=<key> python3 tools/contract_check.py https://你的域名:8443 --key <key>

# 2) 冒烟（18 项，含 Answer 侧证据预算）
python3 tools/smoke.py https://你的域名:8443 --key <key>

# 3) 重放（幂等 + 确定性，不给 MEMORY_SYSTEM_KEY 会 401）
MEMORY_SYSTEM_KEY=<key> python3 tools/replay.py https://你的域名:8443 5

# 4) 持续压测 —— 申报值只看这一项，burst 全过不算数
python3 tools/search_load.py https://你的域名:8443 --key <key> --user-id soak:test \
        --concurrency 8 --duration 180 --nonce
python3 tools/search_load.py https://你的域名:8443 --key <key> --user-id soak:test \
        --mode add --concurrency 8 --duration 120 --nonce

# 5) 压完看计数：api_429 涨得多就退一档；api_give_up > 0 说明重试预算耗尽
curl -s -H "Authorization: Token <key>" https://你的域名:8443/version
```

自签证书联调时加 `SSL_CERT_FILE=<根 CA>`。

---

## 五、评测期（10-31 → 11-04）必须保证的事

- [ ] 机器不关机、不休眠、不自动重启（Windows：电源计划改为「从不」，关掉自动更新重启）
- [ ] 关掉会占带宽的软件（网盘同步、下载、直播）
- [ ] DDNS 能在 1 分钟内更新（把路由器 WAN 口拔了重插验证一次）
- [ ] `.env` 里 `MEMORY_SYSTEM_KEY` 是长随机串，不在任何文档/聊天里出现
- [ ] `docker compose logs` 不带请求正文（只有元数据），日志轮转已开
- [ ] 机器重启后能自愈：`restart: unless-stopped` + Docker 服务开机自启
- [ ] 开跑前 `sqlite3 .backup` 存快照，并在 `RUNBOOK.md` 里写恢复步骤
- [ ] DashScope 余额与 RPM 足够（Full 一次约 ¥0.5，但重试会翻倍）

---

## 六、常见失败与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 平台报 `not publicly reachable` | DDNS 没生效 / 端口映射错 / 证书无效 | 从手机 4G 访问 `/health`，先排除本地 |
| 大面积超时 | 家宽上行被打满 | 降申报并发；确认没被 CDN/穿透限速 |
| `401 invalid credential` | 平台用的是 `Memory System Key`，不是 Leaderboard Key | 核对凭证用途（`research/06` §三） |
| 中途 IP 变了 | 动态 IP 未更新 | DDNS 缩短 TTL；或直接用方案 B |
| 评测中断 | 平台支持断点续跑，同一 `request_id` 幂等 | 不要删库、不要改版本，重启后等平台恢复 |
