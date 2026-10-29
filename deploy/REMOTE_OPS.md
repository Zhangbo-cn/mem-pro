# 远端运维手册：IP 证书 HTTPS / 带宽 / 精排核验 / 加固

> 2026-10-26。对应已上线机器：华为云 Ubuntu 24.04，2 vCPU / 1.7 GiB / 40 GiB，
> `113.44.133.30`，`/opt/mem-pro`，systemd `mem-pro.service`，Caddy 2.11.7 反代 `127.0.0.1:8080`。
> **本手册的每条命令都在文末「§6 校验点」有一行可复制的验收命令**；每一步失败都有回滚。

## 0. 现状与目标

| 项 | 现状 | 目标 |
|---|---|---|
| 协议 | `http://113.44.133.30`（Token 明文） | `https://113.44.133.30`（有效证书，无域名、无备案） |
| 带宽 | 2 Mbit 固定，公网上行 ≈0.25 MB/s ⇒ ≈0.2 req/s | 按流量计费 + 100 Mbit 上限 ⇒ ≈5–10 req/s |
| 精排 | 开着（`qwen3-rerank`），`fails=0` | 保持不变，加自动核验 |
| 申报并发 | Search 2 / Add 2–4（带宽受限） | 带宽放开后 Search 8 / Add 8 |
| SSH | 密码登录开着 | 仅密钥 + 安全组限源 IP |

执行顺序：**§1 → §2 → §3 → §4 → §5**。先做 §1（HTTPS 立刻消除明文 Token），再做 §2（带宽）。

---

## 1. 给公网 IP 签 HTTPS 证书（绕开备案）

### 1.1 原理

Let's Encrypt 自 **2026-01-15** 起对 **IP 地址**正式签发证书（`shortlived` 配置档，有效期 160 小时 ≈ 6.7 天，
Caddy 自动续期）。大陆备案拦截按**域名 / SNI** 生效，纯 IP 请求不受影响，所以这条路不需要域名、不需要备案。

### 1.2 前置检查（在服务器上）

```bash
# Caddy 版本需 ≥ 2.10.2（IPv4 签发修复版本）；实测机为 2.11.7
caddy version

# 80 / 443 必须能被公网访问（80 用于 ACME HTTP-01 校验，别关）
sudo ss -lntp | grep -E ':(80|443)\b'
# 安全组也要放通 80、443（入方向 0.0.0.0/0）

# 时钟必须准，ACME 对时间敏感
timedatectl | grep -E 'synchronized|NTP'

# 本机回环到 mem-pro 正常
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/health
```

### 1.3 备份现有配置

```bash
sudo cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%F)
sudo mkdir -p /var/log/caddy && sudo chown caddy:caddy /var/log/caddy
```

### 1.4 写入新配置

仓库里 `deploy/Caddyfile.ip` 就是成品。**两个关键行缺一不可**：

1. 站点写 `https://113.44.133.30` + `tls { issuer acme { profile shortlived } }`；
2. 全局写 `default_sni 113.44.133.30` —— **最容易漏、漏了必挂**（见 §1.4.1）

```bash
sudo cp /opt/mem-pro/deploy/Caddyfile.ip /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile   # 必须 "Valid configuration"
sudo systemctl reload caddy
```

### 1.4.1 为什么必须有 `default_sni`（2026-10-28 实测踩坑）

证书签下来了、`openssl s_client -servername 113.44.133.30` 能看到 Let's Encrypt 证书，**但 `curl` 用 IP 访问直接报**
`OpenSSL/3.0.13: error:0A000438:SSL routines::tlsv1 alert internal error`（连服务器本机 `curl -k https://127.0.0.1/health` 也一样）。

原因：**RFC 6066 规定 SNI 只能是主机名，不能是 IP**，所以 curl / python requests / 浏览器访问 IP 字面量时**不发 SNI**；
Caddy 按 SNI 选证书，收不到 SNI 就没有证书可给，直接回 `alert internal error`（日志里连一行 handshake 都不留，很容易误判成网络问题）。

修法：全局 `default_sni 113.44.133.30`，让无 SNI 的握手落到这张 IP 证书上。加完立刻 200 / `verify=0`。

> **注意**：不要同时保留 `comp.mem-pro.asia` 那条站点。Caddy 会为它申请证书，而 ACME 校验会被备案策略重置
> （`Connection reset by peer`），日志里会一直刷错误。

### 1.5 观察签发过程

```bash
sudo journalctl -u caddy -f --since -2m | grep -iE 'acme|obtain|certificat|challenge|error'
```

期望 30–90 秒内出现 `certificate obtained successfully`。签发成功后证书落在：

```bash
sudo find /var/lib/caddy/.local/share/caddy/certificates -name '*.crt' | sort | tail -3
```

### 1.6 验证（三项全过才算成功）

```bash
# ① 不加 -k 也必须是 200（自签证书在这里会失败）
curl -sS -o /dev/null -w '%{http_code} %{ssl_verify_result}\n' https://113.44.133.30/health

# ② 证书链与主体
echo | openssl s_client -connect 113.44.133.30:443 -servername 113.44.133.30 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# ③ 80 端口应 308 跳转到 https
curl -sS -o /dev/null -w '%{http_code} %{redirect_url}\n' http://113.44.133.30/health
```

期望：①② 正常、③ 输出 `308 https://113.44.133.30/health`。
`subject` 应为 `CN=113.44.133.30`，`issuer` 含 `Let's Encrypt`，`notAfter - notBefore ≈ 6.7 天`。

然后走业务链路复验（在开发机上）：

```bash
python3 tools/contract_check.py https://113.44.133.30 --key <Key>   # 27/27
python3 tools/smoke.py          https://113.44.133.30 --key <Key>   # 18/18
python3 tools/replay.py         https://113.44.133.30 --key <Key>   # 幂等 + 字节一致
```

### 1.7 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 日志 `dial tcp ... i/o timeout` / `connection refused` | 安全组未放通 80，或 Caddy 未监听 80 | 放通 80；确认配置里有 `http://113.44.133.30` 那一段 |
| `urn:ietf:params:acme:error:...ip...not permitted` | Caddy 版本太旧（<2.10.2）或没写 `profile shortlived` | 升级 Caddy；确认 `profile shortlived` |
| `connection reset by peer` 且 SNI 是**域名** | 备案拦截 | 只用 IP，删掉域名站点 |
| `too many certificates` / rate limit | IP 证书重复申请过多 | 等 24 小时再试；不要反复 reload |
| 证书签下了但 `curl` 报 `SSL: no alternative certificate subject name` | 客户端用域名访问 | 提交给平台的 URL 必须写 `https://113.44.133.30`（IP 字面量） |
| `tlsv1 alert internal error`，`openssl s_client` 却能成功 | **缺 `default_sni`**（客户端不发 SNI） | 全局加 `default_sni 113.44.133.30`，见 §1.4.1 |

### 1.8 续期与监控（6 天有效期，必须盯）

Caddy 会在证书剩余 1/3 时自动续；`renew_interval 15m` 已把检查频率调高。但**只靠 Caddy 不够**：
证书只有 160 小时，一旦续期失败，最容易正好落在 Full 期间断服。因此装一个 systemd timer
（每 30 分钟）做「剩余时间 + 本地握手」双重自检：

```bash
# 服务器上（仓库已带这三个文件）
sudo cp /opt/mem-pro/deploy/mempro-certcheck.sh /opt/mem-pro/deploy/
sudo install -m 644 /opt/mem-pro/deploy/mempro-certcheck.service \
                    /opt/mem-pro/deploy/mempro-certcheck.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now mempro-certcheck.timer
sudo systemctl list-timers mempro-certcheck.timer      # NEXT 应该是 30 分钟内
sudo systemctl start mempro-certcheck.service          # 立刻跑一次
sudo tail -3 /var/log/mempro-cert.log
```

判定与动作（阈值可用环境变量覆盖，便于演练）：

| 剩余时间 | 动作 | 退出码 |
|---|---|---|
| > 7 天 | 只记 OK | 0 |
| < 48 h（`WARN_HOURS`） | `systemctl reload caddy` 触发立即续期 | 0 |
| < 24 h（`CRIT_HOURS`） | `systemctl restart caddy`，systemd 标 failed | 2 |
| < 6 h（`PANIC_HOURS`） | 同上报 CRITICAL，需人工介入 | 2 |

本地 HTTPS 握手失败时也会 reload 一次并等 20 秒复验（`wait_https`），避免把网络抖动误判成证书问题。

演练（不改真实阈值，强制走 CRITICAL 分支，预期 restart Caddy 且退出码 2）：

```bash
sudo WARN_HOURS=9999 CRIT_HOURS=9999 PANIC_HOURS=9999 /opt/mem-pro/deploy/mempro-certcheck.sh; echo $?
```

> 旧的 `/etc/cron.d/mempro-cert` 已删除，避免与 timer 重复 reload。

### 1.9 回滚

```bash
sudo cp /etc/caddy/Caddyfile.bak-$(date +%F) /etc/caddy/Caddyfile
sudo systemctl reload caddy
# 提交给平台的地址改回 http://113.44.133.30
```

评测窗口（11-04）前若证书出现异常，可直接退回 HTTP + IP：官方原文只是「生产环境**建议**使用 HTTPS」，
硬约束是「公网可达、不含凭据」。

---

## 2. 带宽：改按流量计费 + 提速

### 2.1 变更前记录

```bash
# 当前公网上行实测（在开发机上跑，直连 IP，绕过 CDN）
python3 tools/wan_bench.py --url https://113.44.133.30 --key <Key> --images 12 --searches 20 \
  --report /tmp/wan-before.json
```

记下 `miB_per_s` / `req_s` / `projected_min_1711`。**变更后必须用同一命令复测**，否则无法证明提速生效。
工具会 Add 一个临时用户 `wanbench:<时间戳>`，量完记得清（见 §5.3）。

### 2.2 控制台步骤（华为云）

1. 登录控制台 → **弹性公网IP EIP** → 找到 `113.44.133.30` 这条。
2. 右侧 **更多 → 修改计费模式**（部分界面叫「转按流量计费」/「变更计费方式」）。
3. 选择 **按流量计费**，**带宽上限填 100 Mbit/s**（按流量计费时「带宽」只是速率上限，不按带宽收费）。
4. 确认后 1–2 分钟内生效，**公网 IP 不变**，无需重启机器。

> 若界面里没有「修改计费模式」：说明该 EIP 是**包年包月**，需先「转按需计费」，改完再切换计费模式；
> 或者该 EIP 已加入**共享带宽**，要先移出共享带宽。
> 生效判据：EIP 详情页「计费模式」显示 `按流量计费`，且「带宽」显示 `100 Mbit/s`。

**确认单价**：控制台 → 费用中心 → 价格详情 → 弹性公网IP 按流量，一般约 ¥0.8/GB（出网方向）。
**只对出网计费**，入网免费——我们的 Search 响应正好是出网。

### 2.3 变更后验证

```bash
python3 tools/wan_bench.py --url https://113.44.133.30 --key <Key> --images 12 --searches 20 \
  --report /tmp/wan-after.json
```

对照 `/tmp/wan-before.json` 与 `after.json` 的 `mbit` 字段。若仍 ≈2 Mbit，检查：
安全组无 QoS 限制、EIP 计费模式确实变了、`wan_bench` 打的是 IP 而不是域名（域名会被拦）。

### 2.4 成本估算（全部评测跑完）

| 场景 | 查询数 | 平均响应（`MAX_IMAGES=3`） | 出网流量 | 费用 @¥0.8/GB |
|---|---|---|---|---|
| Mem-Gallery | 1,711 | 1.20 MiB | ≈2.0 GB | ≈¥1.6 |
| 多模态 5,000 | 5,000 | 1.20 MiB | ≈5.9 GB | ≈¥4.7 |
| Add 上行（平台→我们，入网免费） | — | — | 0 | ¥0 |
| **合计（含 2 倍余量）** | | | ≈16 GB | **≈¥13** |

即使按 4 倍余量估也在 ¥30 以内，远低于「2 Mbit 跑不完」的代价。

### 2.5 申报并发重算

单次 Search 耗时 ≈ 传输时间 + 0.3 s 计算：

| 公网上行 | 1.20 MiB/次传输 | 单次耗时 | 单并发 req/s | 申报并发（留 3 倍余量） |
|---|---|---|---|---|
| 2 Mbit（旧） | 4.8 s | ≈5.1 s | 0.20 | **2** |
| 20 Mbit | 0.48 s | ≈0.8 s | 1.25 | 4 |
| 50 Mbit | 0.19 s | ≈0.5 s | 2.0 | 6 |
| 100 Mbit | 0.10 s | ≈0.4 s | 2.5 | **8** |

平台按申报值当**上界**校验，报小不吃亏、报大跑超时会被判失败。**以 §2.3 实测为准**，上表只作预估。
回环能力另有冗余（实测 16 并发 76 req/s），所以瓶颈永远在带宽侧。

### 2.6 `MAX_IMAGES` 要不要从 3 改回 6

| | `MAX_IMAGES=3`（现状） | `MAX_IMAGES=6` |
|---|---|---|
| 平均响应 | 1.20 MiB | 2.38 MiB |
| 带宽需求 | 减半 | 翻倍 |
| 对答案的影响 | **未知** | 未知 |

结论：**带宽没放开就保持 3**。若改，`config_hash` 会变（`MAX_IMAGES` 进了指纹），必须重新记录并同步材料，
所以定下来就不要再动。当前 `MAX_IMAGES=3` 的 `config_hash=1db7510cb14b0baf` 已验证过全链路，建议直接冻结。

### 2.7 回滚

控制台同一入口改回 **按带宽计费**，带宽填回 2 Mbit。费用按小时结算，改回来不额外扣费。

---

## 3. 精排保持不变，但要能证明它没降级

### 3.1 为什么保持开启

| 口径 | 精排收益 |
|---|---|
| 按 Search 返回条目评分 | MRR **+0.04**（ATM-Bench 交叉验证：0.784 → 0.861/0.863） |
| 按 Answer 输出评分 | ±0.02（无可测差异） |

两种口径下都不吃亏，且精排**不占上行带宽**（只多一次到 DashScope 的往返）。

### 3.2 核验命令

```bash
curl -s -H "Authorization: Token <Key>" https://113.44.133.30/version \
  | python3 -c "import json,sys;r=json.load(sys.stdin)['retrieval']['rerank'];print(r)"
# 期望：mode=http, model=qwen3-rerank, top=30, blend=0.7, calls>0, fails=0, degraded=0
```

跑 10 次 Search 后再看一次：`calls` 应 += 10 左右（`RERANK_TOP=30` 时只有前 30 名参与精排前的候选择优，
实际 calls 可能少于 10），`fails` 必须仍为 **0**。

### 3.3 401 陷阱（已踩过一次，务必复验）

systemd 的 `EnvironmentFile=` **不展开 `$VAR`**，会把 `RERANK_API_KEY=$DASHSCOPE_API_KEY` 当字面量塞进环境，
`service/dotenv.py` 见到变量已存在便不再展开 → 每次精排 401 → **静默降级**（表现为 `fails` 稳定增长、`degraded=1`，
但接口仍然 200，很难发现）。正确配置：

```bash
# 单元里不要有 EnvironmentFile（只留 Environment=MALLOC_ARENA_MAX=2 之类字面量）
systemctl cat mem-pro | grep -c EnvironmentFile     # 必须是 0
# .env 里精排那段（dotenv 会展开 $DASHSCOPE_API_KEY）
grep -E '^RERANK_(MODE|KIND|MODEL|API_KEY)=' /opt/mem-pro/service/.env
```

### 3.4 若 `fails` 增长

```bash
sudo journalctl -u mem-pro --since -30m | grep -iE 'rerank|401|403' | tail -20
```

| 症状 | 处置 |
|---|---|
| 401 | Key 未展开 / Key 过期：`grep RERANK_API_KEY service/.env`，确认不是字面量 `$...` |
| 超时 / 5xx 偶发 | 正常（内部有重试，`RERANK_FAILS=3` 才降级）；连续增长才处理 |
| `degraded=1` 持续 | 重启服务复位；若复现则临时 `RERANK_MODE=none`（会改 `config_hash`，须同步材料） |

---

## 4. 安全加固

### 4.1 SSH 关掉密码登录

```bash
# 先确认自己的公钥已在 authorized_keys（另开一个会话验证能登录再退出）
sudo install -d -m 700 ~/.ssh
grep -c "$(cat ~/.ssh/id_ed25519.pub 2>/dev/null | cut -d' ' -f2)" ~/.ssh/authorized_keys

# Ubuntu 云镜像里 50-cloud-init.conf 会写 PasswordAuthentication yes，
# 而 sshd 是「先匹配先赢」，所以加固文件必须排在它前面（00- 前缀）
sudo tee /etc/ssh/sshd_config.d/00-hardening.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
EOF
# 同时把 cloud-init 那份里的 yes 改掉，防止以后被覆盖回去
sudo sed -i 's/^PasswordAuthentication yes/PasswordAuthentication no/' \
  /etc/ssh/sshd_config.d/50-cloud-init.conf 2>/dev/null || true

sudo sshd -t && echo "配置语法 OK"          # 语法不过千万别 reload
sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin)'
# 期望：passwordauthentication no / kbdinteractiveauthentication no
sudo systemctl reload ssh
```

**保持一个已登录的 SSH 会话不要关**，另开窗口验证新连接可用后再退出旧会话。

安全组收紧 22 端口：入方向只放通你的固定出口 IP（`x.x.x.x/32`），不要 `0.0.0.0/0`。
可选 fail2ban：`sudo apt install -y fail2ban && sudo systemctl enable --now fail2ban`。

### 4.2 去掉版本指纹

Caddy 默认会回 `Server: Caddy`。在站点块里加一行（`deploy/Caddyfile.ip` 已含）：

```
header -Server
```

业务侧 `/version` 里本来就有 `commit`（方便审核核验），不需要额外隐藏；`X-Server-Version` 保留。

### 4.3 日志卫生与磁盘

40 GiB 盘、Caddy 访问日志按 100 MiB × 5 轮转（`deploy/Caddyfile.ip` 已配置），journald 限容：

```bash
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=500M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
df -h / /var 2>/dev/null | tail -2
```

### 4.4 评测结束后的合规清理

规则要求任务完成后 30 天内删除评测数据（`research/06 §八`）：

```bash
python3 /opt/mem-pro/tools/purge.py --db /data/mem.db --user-prefix 'wanbench:' --dry-run
python3 /opt/mem-pro/tools/purge.py --db /data/mem.db --all --captions
sudo journalctl --rotate && sudo journalctl --vacuum-time=1d
sudo rm -f /var/log/caddy/access*.log*
```

### 4.5 fail2ban：封禁暴力试 Key / 扫路径的 IP（2026-10-29 新增）

仓库公开后会有人扫这台机器（IP 本来就在 Let's Encrypt 的 CT 日志里）。服务本身已鉴权，
未带正确 Key 的请求不会调 embedding，这一步只是**省流量费、降噪**。
规则读 Caddy 的 JSON 访问日志（mem-pro 在反代后面只看得到 127.0.0.1）：

> 前提：Caddy 站点块里要有 `log`（`deploy/Caddyfile.ip` 已含）。**必须带 `format filter … request>headers delete`**，
> 否则请求头里的 `Authorization` / `X-Api-Key` 会明文写进 access.log。
> 2026-10-29 线上实装时发现服务器 `/etc/caddy/Caddyfile` 没有 log 块（access.log 停在 10-28），已补上并 reload。

| jail | 触发 | 封禁 |
|---|---|---|
| `sshd` | 10 分钟 5 次失败 | 1 天 |
| `mempro-auth` | 1 分钟 20 次 401/403 | 1 小时（80/443） |
| `mempro-probe` | 5 分钟 30 次 404/405 | 1 天（80/443） |

平台只调 `/health` `/add` `/search` 且 Key 正确，两条规则都碰不到它。

**部署机保持在 `v1.0.0`，不要 `git pull`**（会改 `/version` 的 commit）。从本机拷文件过去：

```bash
# 本机
scp deploy/fail2ban/mempro.conf deploy/fail2ban/mempro-probe.conf \
    deploy/fail2ban/mempro.local <user>@113.44.133.30:/tmp/

# 服务器
sudo apt install -y fail2ban
sudo install -m 644 /tmp/mempro.conf /tmp/mempro-probe.conf /etc/fail2ban/filter.d/
sudo install -m 644 /tmp/mempro.local /etc/fail2ban/jail.d/
# 把自己的固定出口 IP 加进 ignoreip，防止把自己封掉
sudo sed -i 's|^ignoreip = .*|ignoreip = 127.0.0.1/8 ::1 <你的出口IP>/32|' /etc/fail2ban/jail.d/mempro.local

# 先对真实日志试跑，确认能匹配（401 行数应 > 0 若有人试过错 Key）
sudo fail2ban-regex /var/log/caddy/access.log /etc/fail2ban/filter.d/mempro.conf
sudo fail2ban-regex /var/log/caddy/access.log /etc/fail2ban/filter.d/mempro-probe.conf

sudo systemctl enable --now fail2ban && sudo systemctl restart fail2ban
sudo fail2ban-client status                 # 应列出 sshd, mempro-auth, mempro-probe
sudo fail2ban-client status mempro-auth
```

验证（从另一台机器/手机热点，**不要用你在 ignoreip 里的 IP**）：

```bash
for i in $(seq 25); do curl -sk -o /dev/null -w '%{http_code} ' -X POST https://113.44.133.30/search; done
# 前 20 次 401，之后连接超时 = 已封；服务器上解封：
sudo fail2ban-client set mempro-auth unbanip <该IP>
```

2026-10-29 线上实测：`fail2ban-regex` 对现有日志 auth 命中 6 / probe 命中 11；从本机连发 23 次无 Key 的
`/search`，第 21 次起 80/443 被 nftables reject（SSH 不受影响），`unbanip` 后恢复 200。

再加两项控制台设置：

1. 安全组：22 只放通自己出口 IP；80/443 保持 `0.0.0.0/0`（ACME 续期与平台调用都需要）；其余全关。
2. 费用中心 → 预算管理：给弹性公网 IP 流量设月度预算告警（如 ¥50），防被刷流量。

---

## 5. HTTPS 全量复验 + 冻结提报

### 5.1 复验（全部走 https）

```bash
python3 tools/contract_check.py https://113.44.133.30 --key <Key>     # 27/27
python3 tools/smoke.py          https://113.44.133.30 --key <Key>     # 18/18
python3 tools/replay.py         https://113.44.133.30 --key <Key>     # 幂等 + 5 次字节一致
python3 tools/wan_bench.py      https://113.44.133.30 --key <Key>     # 带宽与吞吐
```

### 5.2 记录版本指纹

```bash
curl -s -H "Authorization: Token <Key>" https://113.44.133.30/version \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['commit'], d['config_hash'], d['max_results'])"
```

把 `commit` 与 `config_hash` 抄进 `SUBMISSION.md` 第 5 项与 `deploy/LOCAL_DEPLOY.md` §7.1。
**任何影响排序/预算的配置改动都会改 `config_hash`**（含 `MAX_IMAGES`、`MAX_RESULTS`、精排开关），
改完必须重新记录并同步材料，否则审核比对不上。

### 5.3 提报字段清单

| 字段 | 值 |
|---|---|
| Add 地址 | `https://113.44.133.30/add` |
| Search 地址 | `https://113.44.133.30/search` |
| 并发（Search / Add） | 按 §2.3 实测填（100 Mbit 约 8 / 8；2 Mbit 则 2 / 2–4） |
| Memory System Key | 私有渠道提交，**不入仓库** |
| 版本指纹 | `commit` + `config_hash` |

---

## 6. 一页速查

```bash
# HTTPS
sudo cp /opt/mem-pro/deploy/Caddyfile.ip /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && sudo systemctl reload caddy
curl -sS -o /dev/null -w '%{http_code}\n' https://113.44.133.30/health          # 200，不加 -k
echo | openssl s_client -connect 113.44.133.30:443 -servername 113.44.133.30 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates

# 精排没降级
curl -s -H "Authorization: Token <Key>" https://113.44.133.30/version \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['retrieval']['rerank'])"

# 带宽
python3 tools/wan_bench.py --url https://113.44.133.30 --key <Key> --report /tmp/wan-after.json

# 业务
python3 tools/contract_check.py https://113.44.133.30 --key <Key>
python3 tools/smoke.py https://113.44.133.30 --key <Key>
```

## 7. 已知坑汇总

1. **systemd `EnvironmentFile` 不展开 `$VAR`** → 精排假开启（§3.3）。
2. **备案拦截按域名/SNI**，按 IP 访问 80/443 正常；域名 + HTTPS 需要备案/CF/隧道，全都不如 IP 证书直接。
3. **IP 证书只有 6 天**，`renew_interval` 与每日自检都要有；Caddy 停止运行超过 6 天会直接失效。
4. **IP 证书不支持 DNS-01**，只能 HTTP-01 / TLS-ALPN-01，所以 80 和 443 必须对外开放。
5. **`MAX_IMAGES` / `MAX_RESULTS` / 精排开关都在 `config_hash` 里**，冻结后不要动。
6. **按流量计费只算出向**，但被刷流量会烧钱：Token 不要把库清空，评测结束后按 §4.4 清理。
7. **Caddy 的 access.log 目录要先建并 chown caddy**，否则 reload 失败。
8. **IP 证书必须配 `default_sni`**：客户端对 IP 字面量不发 SNI，缺这行所有正常 HTTP 客户端都握不上手（§1.4.1）。
9. **申报并发是上界**，报大了平台并发打进来会超时判失败，宁可报小。

## 9. 2026-10-28 持续压测复测（`--duration 180`，线上同款 2C2G）

回环（排除公网带宽，看服务端上限；`--nonce` 每个请求都是新 embedding）：

| 方向 | 并发 | 吞吐 | p50 | p95 | 成功 |
|---|---|---|---|---|---|
| Search | 2 | 8.1 req/s | 225 ms | 380 ms | 1466/1466 |
| Search | 4 | 20.5 req/s | 197 ms | 343 ms | 3696/3696 |
| Search | 8 | 39.2 req/s | 159 ms | 447 ms | 7104/7104 |
| Add | 1 | 6.7 req/s | 128 ms | 285 ms | 1210/1210 |
| Add | 2 | 20.2 req/s | 109 ms | 225 ms | 3642/3642 |
| Add | 4 | 45.3 req/s | 99 ms | 223 ms | 8153/8153 |

公网真链路（合成图 12 张 38 KiB–2.2 MiB，缩图后平均响应 121 KiB）：单并发 **1.10 req/s**、
p50 0.64 s、有效 **0.13 MiB/s** —— 2 Mbit 出网下带宽与时延都还有余量。

结论：**维持 SUBMISSION 的 Search 3 / Add 4 申报**（60–70% 保守口径），不因回环数字放宽。
