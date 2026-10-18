# Add / Search 服务（路线 B：学术榜 + 零 LLM）

纯标准库实现，**不需要 pip install 任何东西**。当前状态：本地契约自检 **27/27 通过**，确定性 5/5 一致。

```
service/  server.py    HTTP 服务（Add / Search / Health）
          embed.py     text-embedding-v4（DashScope）或离线 hash 模式
          store.py     SQLite：记忆 + 向量 + 幂等表
          retrieve.py  dense + BM25 → RRF → 去重 → 可选精排
          budget.py    117,760 token / 28 MiB / 图片配额 三重裁剪
tools/    contract_check.py  11 类 27 项契约自检
          replay.py          幂等 + 确定性检查
```

## 一、拿 text-embedding-v4 的 key（约 10 分钟）

1. 打开 <https://bailian.console.aliyun.com/> → 用阿里云账号登录（需实名，支付宝可完成）。
2. 首次进入会提示**开通百炼（Model Studio）**，点开通，勾选协议。免费额度通常够调试。
3. 右上角头像 → **API-KEY** → 创建 API Key → 复制 `sk-xxxxxxxx`（只显示一次）。
4. 控制台左侧「计费管理」确认已开通按量付费；充 50–100 元足够跑通与压测。
5. 验证（30 秒）：

```bash
export DASHSCOPE_API_KEY=sk-你的key
curl -s https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings \
  -H "Authorization: Bearer $DASHSCOPE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"text-embedding-v4","input":["hello"],"dimensions":1024,"encoding_format":"float"}' \
  | head -c 300
```

看到 `"object":"list"` 与一串 float 就成了。

> ⚠️ 学术榜强制使用 `text-embedding-v4`，不可替换为其他 embedding。价格约 **0.0005 元/千 token**（800M tokens ≈ 400 元）。

## 二、本地跑起来（两种模式）

```bash
cd service

# A. 离线模式：不联网、不花钱、确定性最强，用于开发与契约测试
EMBED_MODE=hash PORT=8080 python3 server.py

# B. 真实模式：接 DashScope
EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx PORT=8080 python3 server.py
```

另开一个终端自检：

```bash
python3 tools/contract_check.py http://127.0.0.1:8080     # 期望 通过 27 / 27
python3 tools/replay.py        http://127.0.0.1:8080 5   # 期望 幂等 ✅ / 确定性 ✅
```

## 三、公网部署（smoke 要求公网可达 + HTTPS）

规则：`URL 中不得包含用户名、密码等凭据，也不得指向私有、回环或链路本地地址`。

最省事的做法（Caddy 自动签证书）：

```bash
# 1) 起服务（建议 systemd / supervisor 托管）
MEMORY_SYSTEM_KEY=$(openssl rand -hex 24) EMBED_MODE=api DASHSCOPE_API_KEY=sk-xxx \
  PORT=8080 nohup python3 service/server.py >/var/log/aml.log 2>&1 &

# 2) Caddyfile
cat >/etc/caddy/Caddyfile <<'EOF'
mem.example.com {
    reverse_proxy 127.0.0.1:8080
}
EOF
systemctl reload caddy
```

然后：

```bash
curl https://mem.example.com/health
python3 tools/contract_check.py https://mem.example.com --key $MEMORY_SYSTEM_KEY
```

**鉴权**：设置 `MEMORY_SYSTEM_KEY` 后，服务接受 `Authorization: Token <k>`、`Authorization: Bearer <k>`、`X-Api-Key: <k>` 三种。这个 key 就是你提交给平台的 **Memory System Key**（自己生成，不要泄露）。

## 四、提交材料时怎么对应

在 `/evaluation` → `Request evaluation access` 里填：

| 表单字段 | 填什么 |
|---|---|
| Leaderboard track | **Academic leaderboard**（选定不可改） |
| System name / Version name | 如 `aml-mem` / `v1.0.0`（与仓库 tag 一致） |
| Add API URL | `https://mem.example.com/add` |
| Search API URL | `https://mem.example.com/search` |
| Authentication | `Authorization: Token` |
| Memory System Key | 上面 `openssl rand` 生成的那个 |
| Public GitHub repository URL | 把这个目录推到公开仓库 |
| Submission notes | 部署说明 + 并发/超时/限流 + 原始作者/技术报告 + **方法改动** + **无 LLM 组件声明**（见下） |

notes 里建议写死这段（降低审核歧义）：

> 本系统不调用任何 LLM：写入侧仅做确定性分块 + `text-embedding-v4` 向量化；检索侧为 dense + BM25 混合，精排使用专用 reranker（FAQ 05 明确 `Reranker 不作限制`）。因此 FAQ 05 的「LLM 相关组件须使用 gpt-4o-mini」条款无适用对象。

## 五、参数（全部走环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` / `HOST` | 8080 / 0.0.0.0 | 监听 |
| `EMBED_MODE` | hash | `hash` 离线 / `api` 真实 |
| `DASHSCOPE_API_KEY` | — | api 模式必填 |
| `EMBED_DIM` | 1024 | 1024 / 1536 / 2048 |
| `MEMORY_SYSTEM_KEY` | 空（不鉴权） | 生产必须设 |
| `DB_PATH` | service/mem.db | SQLite 路径 |
| `RECALL_N` | 50 | 粗排后进精排的候选数 |
| `RERANK_MODE` | none | `http` 时启用 |
| `RERANK_URL` / `RERANK_MODEL` / `RERANK_API_KEY` | — | 精排端点（TEI / DashScope rerank 等） |
| `RERANK_TOP` | 30 | 精排条数（延迟预算） |
| `DEDUP_COS` | 0.97 | 近重复阈值（一事实一卡） |
| `APPEND_OPTIONS` | 0 | 是否把 `options` 拼进 query |

预算常量在 `budget.py`：`TOKEN_BUDGET=109.5k`、`BYTE_BUDGET=28 MiB`、`MAX_IMAGES=6`、`IMG_TOKEN_COST=2500`。

## 六、已知边界（下一步）

1. **精排未接**：默认 `RERANK_MODE=none`，只做 dense+BM25 融合。接上 `bge-reranker-v2-m3` 或 Qwen3-Reranker 后重新标定 `RERANK_TOP`。
2. **无 LLM 结构化抽取**：`text` 里只有原文与规则字段。若后续拿到 `gpt-4o-mini`，在 `split_text` 之后加一层 schema 抽取即可（字段附加，**原文必须保留**——判分对时间粒度要求严格）。
3. **多模态检索只用文本**：图片以 Base64 存下、按需作为证据返回；查询里的图片不参与向量检索（没有图像编码器）。平台会同时给 caption，已足够。
4. **规模**：暴力 cosine 按 `user_id` 分片，单用户分块少（一个会话几百条），足够；若单用户超过 10 万条再换 ANN。
5. **并发**：stdlib 线程服务器够用于 smoke 与中等 Full；Full 前务必压测，申报并发取实测 **60–70%**。
