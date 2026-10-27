# TODO —— 截至 2026-10-26

四个区块：**A 只有你能定 / 需要外部条件**、**B 我可以直接做**、**C 回家测试当天**、**D 冻结与提交**。
每项都写了「完成标准」，避免做成半成品。

> 「SUBMISSION.md 待填」**不是要提交评测内容**。评测题面（Add 的消息、Search 的 query、Answer 判分）
> 由平台驱动，参赛方全程看不到也不用提交 —— 我们只提供两个 HTTP 接口。
> SUBMISSION.md 是**报名表要申报的信息**：系统名/版本、联系人、接口地址、鉴权方式、容量声明、
> 仓库与固定 commit、引用与改动、可公开展示范围、诚信承诺。其中 9 项已由代码与实测填好，
> 剩下的是只有你手上才有的信息（人、机构、域名、冻结后的 commit）。

---

## A. 需要你拍板 / 外部条件

- [ ] **A1 出口方案**：家宽有公网 IPv4 走方案 A，没有走方案 B（见 `deploy/LOCAL_DEPLOY.md`）
      *完成标准*：`curl https://<域名>/health` 从手机 4G 能通
- [ ] **A2 带宽**：2 Mbit/s 的机器跑 Full 要 50 分钟以上；升到 ≥20 Mbit/s 或改家宽
      *完成标准*：`DURATION=180` 持续压测 100% 成功，据此定申报值
- [x] **A3 精排开不开 → 开（2026-10-27 用户决定）**
      已写入 `service/.env`：`RERANK_MODE=http` / `KIND=dashscope` / `MODEL=qwen3-rerank` / `TOP=10`，
      `RERANK_API_KEY=$DASHSCOPE_API_KEY`（dotenv 现支持 `$VAR` 展开，未定义会展开成空串并由 doctor 报错）。
      实测：doctor 探活 ✅；同 query 连查 3 次响应字节一致；单次 Search 本地约 0.16 s（query 命中向量缓存时）。
      *完成标准*：冻结后不再改；如需回滚，`RERANK_MODE=none`。
- [ ] **A4 报名信息**：联系人、机构/团队、可公开展示的信息范围
      *完成标准*：`SUBMISSION.md` 里 4 个空位填满
- [x] **A6 问官方评分口径（2026-10-27 已答复）**
      答复：少于 `top_k` **允许**（是上限，不要求凑足），但"返回较少可能减少有效证据，不能保证对得分没有影响"；
      统一多模态 Answer 路径会消费按协议返回且成功解析的图片，**没有全局固定的最多几张**（取决于尺寸/detail/上下文预算）。
      → 决定：`MAX_RESULTS=0`（不额外截断，上限即 `top_k`）；`MAX_IMAGES=6` 保留为**自设**带宽取舍。全文见 `research/07` 附录 A。
      原话可直接复制：「Mem-Gallery 的 F1 是在统一 Answer 输出上计算，还是在 Search 返回条目上计算？
      Search 返回条数少于 top_k 是否扣分？」
      *完成标准*：拿到书面答复存入 research/07；答案口径 → `MAX_RESULTS=20`，检索口径 / 不明 → 保持 0
- [ ] **A5 Memory System Key**：生成 32 位随机串，只走私有渠道提交
      *完成标准*：`/version` 能通、仓库里搜不到该串

## B. 我可以直接做（不依赖外部条件）

- [x] **B1 弱项分类提升（已完成 2026-10-26，结论是"别调了"）**
      试了三条规则 + 一条阈值：日期匹配（+0.004，只命中 14/1711）、更新类新鲜度（KR −0.033）、
      多样性 MMR（互有胜负，VR −0.033）、低分弃答阈值（净 −0.013）。**全部默认关闭**，
      代码与实测数字留在 README §十七。
      *真正的差距*（`--oracle` 全量 1,711 条诊断）：检索完美时 F1 0.527→0.705，显著差距只在
      VR(+0.44)/VS(+0.36)/TTL(+0.28)/AR(+0.16)——是**证据集形状**问题，不是打分问题。
      `MAX_RESULTS=20` 按答案口径 F1 +0.016，但按检索口径 R@100 减半 → **默认保持 0，待官方答复（A6）**
- [x] **B2 端到端答案评测（已完成 2026-10-26）**：`tools/e2e_proxy.py`，`qwen-flash` 作答，1,711 条全量 × 5 档 top_k
      结论：答案在 **10–20 条饱和**（F1 0.527 @10、0.521 @20、0.505 @100）；TTL 类被长提示拖垮（0.33→0.17）；
      精排在答案端**无可靠收益**（±0.02，互相有胜负）→ Full 建议关精排；新增 `MAX_RESULTS` 开关
      *花费*：18.0M token ≈ ¥3（比预估的 ¥20–40 便宜 10 倍）
      *未完成*：`MAX_IMAGES` 取舍量不了 —— 仓库里没有真实图片字节，只能等 Full 或自备图集
- [ ] **B3 gzip 响应**（可选）：客户端带 `Accept-Encoding: gzip` 时才压缩，图片 base64 压不动但文本能压
      *完成标准*：带宽受限部署下响应字节明显下降，契约 27/27 不受影响
- [ ] **B4 CI 补全**：加 docker build + contract + 短时压测
      *完成标准*：GitHub Actions 绿

## C. 回家测试当天

- [ ] **C1 量网络**：公网 IP / 上行带宽 / 端口（`deploy/LOCAL_DEPLOY.md` §零）
- [ ] **C2 部署**：`docker compose --profile tls up -d --build`
- [ ] **C3 外网三件套**（手机热点跑）：contract 27/27、smoke 18/18、replay 一致
- [ ] **C4 持续压测**：`--duration 180`，Search 与 Add 各一档 → 定申报并发
- [ ] **C5 记录实测值**：把数字发我，我更新 README §十二 与 SUBMISSION §四

## D. 冻结与提交

- [ ] **D1 冻结**：`git tag v1.0.0`，镜像 tag 与 commit 对齐
      *完成标准*：线上 `/version` 的 `commit` + `config_hash` 与 `SUBMISSION.md` 一致
- [ ] **D2 空库快照**：`make backup DB_PATH=/data/mem.db BACKUP=/data/snapshot-empty.db`
- [ ] **D3 提报**：Evaluation 页面填表（Key 次日 19:00 前发放）
- [ ] **D4 Smoke**（每小时限 1 次，本届每赛道 30 次）
- [ ] **D5 Full 前置检查** → 提交 Full（需 0.5–2 天，11-04 23:59 关闭）
- [ ] **D6 值守**：`make watch URL=... KEY=... FEISHU=...`，按 `RUNBOOK.md` §二 盯六个指标
- [ ] **D7 跑完**：归档 `/version` `/stats`，`tools/purge.py` 按合规删数据

## 时间线

```
10-26  B1/B2（不依赖外部条件，现在就能做）
10-27  回家：C1–C5，A1/A2 定稿
10-28  A4 填材料，B3/B4 收尾
10-29  D1 冻结 → D2 快照 → D3 提报
10-30  D4 Smoke（留一天修）
10-31  D5 Full（材料截止日，Full 0.5–2 天）
11-01~04  D6 值守 → D7 归档
```
