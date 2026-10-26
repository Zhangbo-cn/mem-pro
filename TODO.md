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
- [ ] **A3 精排开不开**：我给的数据是「开」（掉吞吐 0，确定性已验证，+0.04 MRR）
      *完成标准*：`.env` 里 `RERANK_MODE` 定稿，冻结后不再改
- [ ] **A4 报名信息**：联系人、机构/团队、可公开展示的信息范围
      *完成标准*：`SUBMISSION.md` 里 4 个空位填满
- [ ] **A5 Memory System Key**：生成 32 位随机串，只走私有渠道提交
      *完成标准*：`/version` 能通、仓库里搜不到该串

## B. 我可以直接做（不依赖外部条件）

- [ ] **B1 弱项分类提升**：TR 0.57 / CD 0.56 / KR 0.54（代理榜）
      做法：解析问题里的显式日期（`2024-06-17`）与相对时间（"上周/上次"），对 `created_at` 加权；
      CD（计数）类同时返回新旧版本，让 Answer 能自己数。
      *完成标准*：a/b 两半 MRR 都不降且对应类别提升，否则回滚
- [ ] **B2 端到端答案评测**：现在只量召回，量不到答案正确率（AR 类没 clue 完全评不了）
      做法：`tools/e2e_proxy.py`，用 `gpt-4o-mini` 在召回证据上作答，评 top_k / MAX_IMAGES 取舍
      *成本*：约 ¥20–40（1,711 条 × 一次调用）
      *完成标准*：给出 `top_k` 与 `MAX_IMAGES` 的推荐值，附分类准确率
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
