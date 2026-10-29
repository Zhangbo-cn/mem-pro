# 16 结构化记忆 / 知识图谱：评审与实施计划（2026-10-29）

> 本文是对「mem-pro 要不要加知识图谱」一版方案的复核和落地计划。
> **前提：本期（第二期）不改线上排序。** 第 2 次 Full 必须在首次 Full 完成 30 天后发起，而评测 11-04 截止，
> 所以本期实际只有一次 Full。下文全部工作都在冻结之后、`feat/structured-memory` 分支上做，服务对象是
> 下一期和产品形态。

---

## 一、对原方案的评审

### 1.1 判断正确、保留的部分

| 结论 | 依据 |
|---|---|
| 现状是「扁平分块 + hybrid 单跳检索 + 确定性规则后处理」，没有实体同一性和版本 | `store.py:31` 只有 9 列；`retrieve.run` 只用 text/vec/created_at |
| 本期不做图 | 冻结 + 只有一次 Full；`research/13` E2 |
| 「同一性 + 版本」比「图」更关键，建议按 0→1→2 分层 | 成本递增，每层可单独回滚 |
| 工程约束：开关进 `retrieve.info()` 和指纹、默认 off、可从原文重建、purge 级联删除 | `tests/test_unit.py:380` 有指纹覆盖测试；`tools/purge.py` 目前只删 memories/idem/captions |

### 1.2 问题（按严重程度排）

1. **证据被说过头了。**
   - E2（Linked < Piled）讲的是 A-Mem 用 LLM 自动连边这一种做法，推不出「知识图谱普遍负收益」。
   - oracle 诊断里 KR/CD 的 n=81，95% CI ±0.14～0.15。这只能说明「这个样本量测不出差别」，
     不能说明「没有提升空间」。而且回答模型用的是 `qwen-flash`，不是平台统一的回答模型。
   - 正确的说法是：**本地证据不支持做图，同时也不足以排除图的作用**。
2. **SGM 的 +20% 能不能迁移过来没有验证。** ATM-Bench 的 SGM 增益来自照片/邮件自带的
   结构化元数据（时间、GPS、地点、发件人）。Mem-Gallery 是「人和助手聊话题，配图带 caption」，
   几乎没有可以填的槽位。KR 题问的是「Julian 现在的立场」，不是 `slot=value` 形态。
   把 +20% 当成预期收益缺乏根据。
3. **slot 版本闭合跟已有实测冲突。** README §十七：`RECENCY_RULE`（抬新压旧）让 KR 下降 0.033，
   原因是「最新一条往往不是陈述最完整的一条」。原方案的 `valid_to` 闭合如果用来**降权**，就是把同一个错误再犯一遍。
   修正：版本信息**只用来标注**，不用来降权（见 P4）。
4. **LLM 抽取的四个工程问题都没处理。**
   - 确定性：gpt-4o-mini 即使 temperature=0，输出也不完全稳定。抽取结果一旦进了排序，
     字节级复现和 `replay` 就会失效，必须先落库再用，而且只抽一次。
   - 合规：不能「跨样本共享状态」。抽取缓存的 key 必须带 `user_id`，不能像 caption 那样只按内容哈希。
   - 写后可见：Add 是同步接口，抽取必须在 200 返回之前完成，Add 延迟要增加 1–3 秒。
   - 配置：本机 checkout 里没有 `service/.env`，OpenAI Key 和 CAPTION 的实际开关状态要在部署机上确认。
5. **没有区分「检索文本」和「返回文本」。** 现在一份 `text` 同时用于 embedding、BM25 和返回。
   卡片要么只用于索引（返回仍是原文），要么也给回答模型看（返回里加元数据头）。
   这两种做法的收益来源不同，要分开测。原方案把它们混在了一起。
6. **漏掉了真正的大头。** 48% 的差距在 VR/VS/TTL，诊断是「证据集形状」。其中跟「结构」有关的是
   **按会话/日期成组召回**（`research/13` D6 证据簇），不是实体图。实测数据：
   - 1,711 题里只有 54 题在问题中提到日期/月份/会话；
   - 现有 `query_dates` 在 77 条含日期词的问题里只识别出 15 条，`late-July` / `early-July` / `August` 这类写法都识别不到。

   这个方向便宜，但量很小，要先做 oracle 判断值不值得做。
7. **缺少验收方法。** 之前的 a/b 分半只看点估计。新方案要求配对 bootstrap CI，
   并且每一层都要有开工前的 oracle 判断和失败时的终止条件。

**总评**：方向对（本期不做，先做结构再考虑图），但原方案对收益估得偏乐观，对工程风险估得偏轻，
也没有做「先证明值得做再动手」的 oracle 步骤。下面的计划补上这些。

---

## 二、总原则

1. **先用 oracle 判断有没有收益，再写服务代码。** 每层开工前先回答一个问题：假设这层结构完美，端到端 F1 能涨多少？
   CI 下界 ≤ 0 就不做。
2. **结构只派生、不当真理源。** 原文和时间戳一字不改（`research/14:204`）。所有结构都能用 `tools/reindex.py` 从 memories 重建。
3. **只加候选、只加小分，不替换融合分。** tie-break 保持 `score desc, created_at desc, id asc`。
4. **每个开关默认 off**，全部写进 `retrieve.info()` 和指纹测试的 knobs 列表。
5. **按 user 隔离**：新表主键都带 `user_id`；缓存 key 带 `user_id`；purge 级联删除。

---

## 三、阶段计划

### P0 冻结期（10-29 → 11-04）：只做不影响排序的事

| # | 任务 | 完成标准 |
|---|---|---|
| 0.1 | 按 TODO D1–D7 完成冻结、Full 和值守 | Full 完成，`/version` 与 SUBMISSION 一致 |
| 0.2 | 本文档**不提交到 main**（提交会改 HEAD，跟申报的 commit 对不上） | Full 受理后再合入 |
| 0.3 | Full 结束后从冻结 tag 拉 `feat/structured-memory` 分支 | 分支上 `make test`、`make check` 全绿 |

### P1 度量基建（约 2 天，不改服务）

| # | 任务 | 文件 | 完成标准 |
|---|---|---|---|
| 1.1 | 配对比较工具：读两份 `--report`，按题配对，输出每个类别的 ΔMRR / ΔR@10 / ΔF1 及 95% bootstrap CI（10k 次重采样，固定 seed） | `tools/compare.py`（新） | 同一份报告和自己比较，CI = [0,0] |
| 1.2 | `gallery_proxy` / `e2e_proxy` 的报告输出逐题结果（id、类别、排名、F1） | 两个 proxy | 1.1 可以直接消费 |
| 1.3 | 结构探针集（固定、只读）：Mem-Gallery 中含时间/会话指代的 54 题、KR 81、CD 81、多会话题 117；ATM-Bench Hard 31 题（平均 6.3 条证据）；ATM 邮件切片 381 题 | `tools/probes.json`（生成脚本 + 产物） | 字节稳定，可重复生成 |
| 1.4 | 合成能力集（只用于单元测试，**不用于调参**）：别名同一性、更新链、两跳关系、规则注入，各 20 条 | 扩展 `tools/gen_eval.py` | 有 gold，`make test` 可以跑 |

### P2 结构 oracle（约 1 天，约 ¥5，决定后续做哪些）

用 `e2e_proxy` 的回答链路，把检索结果换成「假设某种结构完美」时会得到的证据集：

| oracle | 构造方式 | 对应层 |
|---|---|---|
| O-scope | 问题有时间/会话指代时，只保留 gold 会话（或日期）内的记忆，再取 top-10 | P3 |
| O-cluster | top-1 命中后，把同会话里含图片的轮次全部补进来（数图、选图题） | P3 |
| O-card | 证据前面加上元数据头 `[2024-07-21 · D3 · image×2]` | P4 |
| O-version | KR/CD 题里，给 gold 证据标注「这是该话题最后一次表态 / 与 Dx:y 矛盾」 | P5 |

**是否继续**：某层对应 oracle 在目标类别上的 ΔF1 CI 下界 > 0，而且（ΔF1 × 题量）≥ 全量 F1 +0.005，才开这一层。
没达到就在本文档记一笔「不做」，附上数字。

### P3 Layer A：会话/日期作用域（不用 LLM，约 3 天）

适用条件：O-scope 或 O-cluster 通过 P2。

| # | 任务 | 文件 | 说明 |
|---|---|---|---|
| 3.1 | `query_scope(text)`：识别 `YYYY-MM-DD`、`early/mid/late-Month`、单独月份、`first/last/previous session`、「那天/上次」 | `retrieve.py` | 单测覆盖 P1.3 的 54 题，召回率 ≥ 90% |
| 3.2 | 记忆侧作用域键：`day = created_at[:10]`，再加 `session_id`。需要 `fetch_user` 多返回 session_id，并建索引 `(user_id, session_id)` | `store.py` | 迁移用 `PRAGMA user_version` |
| 3.3 | `SCOPE_MODE=off|bias|expand`。bias：作用域内的记忆加 `SCOPE_WEIGHT`。expand：top-1 所在作用域内含图片的记忆补进候选，最多 `SCOPE_FANOUT` 条 | `retrieve.py:fused_scores` / `run` | 只加分不扣分，跟 `DATE_WEIGHT` 合并成一套逻辑 |
| 3.4 | 新开关加进 `info()` 和 `test_config_hash_covers_every_ranking_knob` | `retrieve.py`、`tests/test_unit.py` | 测试通过 |

**终止条件**：a、b 两半在目标类别上方向不一致，或 ALL R@100 下降超过 0.003。

### P4 Layer B：结构化记忆卡（gpt-4o-mini，约 5 天）

适用条件：O-card 通过，或 ATM 邮件切片上 SGM 对 DM 的离线对照（4.5）为正。

| # | 任务 | 说明 |
|---|---|---|
| 4.1 | 新表 `cards(mem_id PK, user_id, schema_ver, card_json, itext, model, prompt_hash, created_at)` | 最多 10 个字段：`t_event, time_raw, session, modality, entities[], place, topic, attrs{}, ocr, raw_ref`。`time_raw` 原样保留 |
| 4.2 | `service/card.py`：先用规则（日期、稀有词、大写串、型号）抽，再用 LLM 补；JSON schema 校验；失败就退回纯规则卡 | 结构照抄 `caption.py`：`CARD_MODE=off|rule|llm`，`CARD_STRICT`，强制模型白名单 gpt-4o-mini |
| 4.3 | 缓存 key = `(user_id, sha256(text), prompt_hash)`，同一个 user 内只抽一次，抽完落库，Search 不调用 LLM | 解决确定性和合规问题 |
| 4.4 | 两种用法分开开关：`CARD_INDEX=0/1`（itext 参与 BM25 / 拼进 embedding 输入，返回仍是原文），`CARD_HEADER=0/1`（返回内容前加一行元数据头） | 分别跑 A/B，不混测 |
| 4.5 | 离线对照：ATM 邮件切片 381 题，同一检索器，DM（现状）对 SGM（卡片文本） | `atm_proxy.py --card` |
| 4.6 | `tools/reindex.py`：从 memories 重建 cards，校验 `raw_ref` 能对应回原文 | 重建两次，结果逐字节相同（规则模式） |
| 4.7 | 成本和延迟：Mem-Gallery 全量约 9.6k 段，输入约 3.8M token、输出约 1.4M token，按 gpt-4o-mini 价格约 $1.5。Add p95 增加不超过 3 s | 用 `make load` 实测 |

**终止条件**：
- `CARD_INDEX` 与 `CARD_HEADER` 都没有在任何目标类别上拿到 CI 下界 > 0，或者
- Add p95 超过 5 s。

满足任一条就关掉，只保留规则卡用于产品展示。

### P5 Layer C：实体与版本标注（偏产品，约 5 天）

适用条件：O-version 通过，或者产品有明确需求（跨会话同一性、规则必须召回）。

| # | 任务 | 说明 |
|---|---|---|
| 5.1 | 表 `entities(user_id, id, name, aliases_json, type)`、`mentions(user_id, mem_id, entity_id, conf)`、`facts(user_id, entity_id, slot, value, mem_id, seq, superseded_by)` | 主键全部带 user_id |
| 5.2 | 实体消解：归一化 → 按稀有 token 分块 → 精确/别名匹配 → union-find。**宁可漏合并，不可错合并**：合成集上 precision ≥ 0.98，recall 不设要求 | 用 P1.4 的合成集测 |
| 5.3 | 版本**只标注不降权**：`superseded_by` 只出现在返回内容的元数据头里，比如 `(later: Dx:y, 2024-08-02)`，不参与打分 | 避开 §十七 的 KR −0.033 |
| 5.4 | 实体候选扩展 `ENTITY_EXPAND=off|on`：query 命中的实体，每个最多补 3 条提及 | 候选总增量不超过 30% |
| 5.5 | 规则/约束钉选（产品专用，`PIN_RULES`，比赛配置一律 off） | 不进比赛指纹默认值 |

### P6 Layer D：关系边 + PPR（**只定义触发条件，不排期**）

满足**全部**条件才开工：
1. 有一个目标基准（例如下一期文本赛道的 LoCoMo multi-hop 类），其「完美两跳证据」oracle 的 ΔF1 CI 下界 > 0；
2. P5 的实体消解在真实数据上 precision ≥ 0.95；
3. 单用户记忆量到 10³ 以上（官方 user_id 是会话级，目前只有几十到几百条，图上做 PPR 基本没有意义）。

做法（届时再细化）：`edges(user_id, src, rel, dst, mem_id, seq)`，单次 PPR（HippoRAG），只作为候选扩展和小幅先验。

---

## 四、所有阶段通用的验收门槛

| 维度 | 门槛 |
|---|---|
| 质量 | 目标类别 ΔF1（e2e）或 ΔR@10（proxy）的 95% CI 下界 > 0，a、b 两半方向一致 |
| 不伤害 | ALL MRR 下降 ≤ 0.005，R@100 下降 ≤ 0.003，AR 的 EM 不降 |
| 交叉验证 | ATM 邮件切片结果不为负 |
| 工程 | `make test`、`make check`（契约 27/27 + replay 字节一致）在开关 on/off 下各跑一次 |
| 性能 | Search p50 增加 ≤ 20 ms；Add p95 增加 ≤ 3 s（LLM 层），规则层 ≤ 50 ms |
| 合规 | purge 级联删除新表；新增缓存 key 带 user_id；日志不落正文 |
| 文档 | 每个实验的结论（包括「不做」）写回 README，格式同 §十七 |

---

## 五、时间线（Full 结束后）

```
W1  P1 度量基建 + P2 结构 oracle      → 产出 go/no-go 表
W2  P3 会话/日期作用域（若 go）
W3–W4  P4 记忆卡（若 go），先做 4.5 离线对照再改服务
W5+ P5 实体与版本（若 go 或有产品需求）
P6  只在满足触发条件后排期
```

预算：P2 + P4 的 API 调用合计 < ¥50（参考 B2 全量 e2e 只花了 ¥3）。
