# 14 · 第二期官网复核与「参赛指南」交叉核对

- 生成时间：2026-10-18
- 输入：① 用户提供的官方公众号文章《倒计时 2 天｜Agent Memory Challenge 2026 第二期参赛指南》（**发布于约 2026-09-18**，即开评前 2 天）；② 官网 `agentmemories.ai` 与 `agentmemoryleaderboard.ai` 实抓；③ 官方仓库 `AML-memory/agent-memory-leaderboard`（⭐1110）
- 关联：`05`（技术栈）、`06`（材料清单）、`07`（待澄清）、`13`（方向评估）
- 方法与核验：
  - `curl -sSL` 实抓；`/`、`/rules`、`/evaluation`、`/api-guide` 均为 **223,162 B 同一 SPA 外壳**（客户端路由），`/competition` 为 **35,123 B 独立页**
  - 两域名 `/competition` 做**逐 token diff** → **内容完全相同**，唯一差异是 Cloudflare 挑战脚本注入（agentmemories.ai 35123 B vs agentmemoryleaderboard.ai 34185 B）
  - 官方仓库经 `api.github.com` 实测（⭐1110、无 LICENSE 文件、pushed 2026-08-27、`api_config.py` + `data/` 六个基准目录）
  - 已读到 `data/locomo-refined/pipeline.py` 的**完整 Answer 提示词与 CORRECT/WRONG 判分规则**（见 §4）

---

## 0. 结论先行

1. **官网内容与公众号文章不冲突**，但**官网比文章多出一批硬参数**——其中三条会直接改变我们的实现方案：**Answer 上下文只有 117,760 输入 token（超出按返回顺序截断前缀）**、**Search 响应 ≤30 MiB 且图片必须内联 Base64**、**开源榜 Add 侧 LLM 被锁定为 `gpt-4o-mini`**。
2. **文章是 9/18 的发布稿，不是当前状态**：官网已进入 "Cycle 2 in progress"，Evaluation 页面出现 8 项 Full 前置勾选、并发申报字段与 `full / light / smoke / scriptmem` 四种模式（后两者无任何文档）。
3. **最要紧的时间算术**：第二次 Full 须在首次 Full **完成满 30 天**后发起，而评测 **11-04** 停止 → 今天（10-18）才启动的队伍，**本届不可能有第二次 Full**。文章那句"计划两次 Full 的团队应尽早完成第一轮"对本届后进队伍**已经失效**。
4. **最大的信息缺口没变**：多模态 7 个数据源只公开了 2 个（ATM-Bench / Mem-Gallery），另外 5 个**官网、仓库、文章都没写**。

---

## 1. 三来源交叉核对

| 项 | 公众号文章 | 官网（实测） | 我们的文档 | 判定 |
|---|---|---|---|---|
| 三赛道 / 两组别 | ✅ | ✅ | ✅ | 一致 |
| 时间（9.20 / 10.31 / 11.04 / 11 月中） | ✅ | ✅ | ✅ | 一致 |
| 奖金 ¥150,000；每赛道 2 万/2×8 千/3×3 千/5 千 | ✅ | ✅（中文） | ✅ | 一致；**英文页写 "US$22,000"** ⚠️ 与 150,000 CNY 不符（按中文为准） |
| Smoke 30 次 / 每小时 1 次 | ✅ | ✅ | ✅ | 一致 |
| Full 每 Key 每赛道 2 次、第 2 次 30 天后 | ✅ 说"**成功完成** 30 天后" | ✅ 说"first Full run is **completed**" | ✅ | 一致（措辞微差） |
| Top K 固定 100 | ✅ | ✅ | ✅ | 一致 |
| 五项接入要点（隔离/写后可见/幂等/返回结构/只返回证据） | ✅ **幂等与"选项字母"仅此处出现** | ✅ 契约页有 request_id 语义，但**未用"幂等"一词** | ⚠️ `05` 有四条，**缺幂等** | 🆕 **文章补了幂等** |
| 多模态"必须正确解析图文数组，并说明是否使用原图/OCR/caption/是否返回图片证据" | ✅ | ❌ **全文无此句** | ⚠️ 已标"公告口径" | 🆕 文章是唯一来源，仍非官网 |
| 多模态媒体限制（10 MiB / 30 MiB / Base64） | ❌ | ✅ **Integration Guide** | ❌ **完全没有** | 🆕 **重大遗漏** |
| Answer 上下文预算 117,760 token | ❌ | ✅ Runtime Rules | ❌ **完全没有** | 🆕 **重大遗漏** |
| Add 重试上限 32 次 / 429 Retry-After ≤60s | ❌ | ✅ Runtime Rules | ❌ 只有"有界重试" | 🆕 |
| 开源榜 Add 侧 `gpt-4o-mini` | ❌ | ✅ Full 8 项勾选第 3 项 | ⚠️ 见 FAQ 05 冲突 | 🆕 **部分解决 `07` 的 P0** |
| `light` / `scriptmem` 模式 | ❌ | ✅ 仅 Evaluation 下拉框 | ❌ | 🆕 无文档 |
| 第一期 136 队 / 67 框架 / 30 万点击 | ✅ | ❌ 未见 | ⚠️ 原标"用户转发" | 升级为"官方公众号口径" |
| "不设组队要求" | 说"支持个人/团队/跨单位组队" | ✅ **明确"不设组队要求"** | ❌ | 🆕 |
| 多模态是"第二期引入" | ✅ | 首页 Leaderboard Preview 的 **Cycle 1** 就有 `Multimodal` 标签 | ⚠️ | ❓ 待查（若一期有多模态成绩，就是现成对手基线） |

---

## 2. 十条注意要点（按优先级）

### P0-1 本届**只有 1 次 Full**（不是 2 次）

- 规则 ✅：`第 2 次须在首次 Full 完成后 30 天发起`；评测 11-04 23:59 停止。
- 算术：要赶在 11-04 前发起第 2 次，首次 Full 须在 **10-05 前完成**。今天 10-18，**已过**。
- 含义：**Full 是一次性考试**。失败/取消可断点续跑（不计次数 ✅），但"分数低"不可重来、不可换版本（`受理后不得更换版本`）。
- 该怎么做：把"能不能跑"与"跑得好不好"彻底分离——**本地契约校验器 + 本地代理评测（`13` 的 D10）必须在 smoke 之前就位**，不要把 smoke 当调试循环。

### P0-2 Answer 上下文只有 **117,760 输入 token**，且**超出部分按返回顺序截断前缀**

官方原文 ✅（Runtime Rules）：

> `The shared 128,000-token Answer window reserves 8,192 output tokens and 2,048 safety tokens, leaving 117,760 input tokens. If needed, Answer keeps a token-counted prefix of Search candidates in returned rank order; the question, options, instructions, and archived Search response remain intact.`

- **这是本周期最重要的一条新信息**：你返回的 `data[]` 不是"都给 Answer 看"，而是**从前往后装，装不下就丢尾部**。
- 定量：按 ATM-Bench 论文实测口径，2 MP 图片约 **2,500 visual tokens/张**。
  - 纯文本条目（~50 token）→ 100 条 ≈ 5k token，远未超
  - 含图条目（~2,500 token）→ **预算只够 ≈ 45 张**
  - 混合 20 图 + 80 文本 ≈ 54k token，安全
- 含义：① **顺序 = 生杀顺序**（`11` 的"证据优先序"从"评分偏好"升级为"物理存活性"）；② 尾部条目是纯浪费（占带宽、增延迟、不参与评分）；③ 与 `13` 的 D4 完全同向，且给出了**硬上限**。
- 该怎么做：把预算写进排序器——**"前 k 条总 token 估算 ≤ 110k"** 作为返回前的最后一道断言；图片条目按"必要性"配额（例如 ≤6 张/响应），不要按"召回多少给多少"。

### P0-3 Search 响应总量 **≤30 MiB**，单图 ≤10 MiB，图片**必须内联 Base64**

官方原文 ✅：

> Add：`JPEG, PNG, or WebP; up to 10 MiB decoded per image and 30 MiB per Add request.`
> Search：`Up to 10 MiB decoded per image and 30 MiB across one Search response.`
> 两处均：`Use an inline Base64 Data URI. Remote image URLs are not accepted.`

- **远程图片 URL 不被接受**——这条直接否掉"图片存对象存储、返回 URL"的常见做法。
- Base64 膨胀 ≈ **×1.33**，所以 30 MiB 响应 ≈ 22.5 MiB 原始字节 ≈ **约 40–60 张 2 MP JPEG**。
- 巧合但重要：**token 预算（≈45 张）与 MiB 预算（≈40–60 张）在同一个量级**——两条独立约束把"百条带图"的可能性同时封死。
- 含义：返回图片 = **自付带宽**（官方 ✅ 参赛方承担带宽），且换不到额外分数（尾部会被丢）。
- 该怎么做：图片按"证据必要"逐条决策；同一事实只留一张代表图；对图片做**降采样到答案够用的分辨率**（答案不需要 2 MP 时，压缩直接省 token 与 MiB）。

### P0-4 Add 必须**按 `request_id` 幂等**，重试上限 **32 次**

- 官方文章 ✅：`保证幂等。平台重试时会复用同一个逻辑请求。参赛服务应以 request_id 处理幂等，避免重复写入污染结果。`
- 官方 Runtime Rules ✅：`at most 32 request attempts. The same logical write keeps the same request_id and payload.`
- 含义：网络抖动 → 平台重试 → 若你不去重，**同一段记忆被写入最多 32 次** → 前 k 被同一内容占满（正是 `13` 的 M2 失效模式，且是自找的）。
- 该怎么做：写入表加 `(user_id, request_id)` 唯一约束 + 幂等短路（命中则直接回 `success:true` 并回显原三个 ID）。**这条在 `05`/`06` 里缺失，必须补进设计。**

### P0-5 开源榜 **Add 侧 LLM = `gpt-4o-mini`**（第二处独立证据）

官方 Full 前置勾选 ✅ 第 3 项原文：

> `Add/Search model rules are understood`
> `Open-source Methods entries are expected to use gpt-4o-mini during Add. Commercial Products entries have no Add/Search model restriction.`

- 这**基本关闭了 `07` §一的 P0 冲突**：FAQ 05 的"学术榜 ≈ 开源方法榜"对应关系**被这一句实际确认**（至少对 Add 侧 LLM）。
- 但**注意两点保留**：
  1. 措辞是 `expected to`（期望），不是 `must`；
  2. 这里**只提 Add 侧 LLM**，**没有复现**"Embedding 须 `text-embedding-v4`"，也没提 Search 侧。→ embedding 约束仍只有 FAQ 05 一处来源。
- 含义：`13` 的 D1（结构化记忆卡）本来就用 `gpt-4o-mini` 抽取 → **无需改方案**；但**不能用更强的模型做抽取**，这进一步强化"schema 设计 > 模型能力"的结论。
- 该怎么做：caption/抽取统一走 `gpt-4o-mini`（它原生支持图像输入 ✅）；OCR 不受限（不是 LLM 组件），可自由选。

### P0-6 并发是**申报即承诺**，且平台 429 会等 60 秒

- Evaluation 表单 ✅ 有 `Max add concurrency` / `Search concurrency` 两个必填；勾选项 ✅ 第 4 项：`Capacity ... is prepared for the selected track's Full evaluation scale and the entered Add/Search concurrency limits.`
- Runtime Rules ✅：`For HTTP 429, the platform honors a valid Retry-After value up to 60 seconds; when absent, it waits 60 seconds.` + `Advertised concurrency is an upper bound, not a guaranteed request rate.`
- 含义：**申报过高 → 实际扛不住 → 大面积 429 → Full 超时/失败；申报过低 → 平台按低并发调度 → 长时间跑不完**。这是个双向陷阱，且**只有一次机会**。
- 该怎么做：先用本地压测把**稳定承载**测出来（含 embedding API 的并发上限，这才是真瓶颈），申报值取实测 **60–70%**；并保证 429 时正确返回 `Retry-After`。

### P1-7 官方仓库公开了 **Answer 提示词与判分规则**——三条规则改写设计

`AML-memory/agent-memory-leaderboard` 的 `data/*/pipeline.py` 是**公开的评测契约**（不含数据）。从 `locomo-refined/pipeline.py` 实读到：

| 规则（原文摘录） | 对设计的含义 |
|---|---|
| `For list questions, include all required items and **no extras**.` | 判分：`gold = A,B,C；gen = A,B,C,D → WRONG`（**多答即错**）→ **噪声证据会被答成多余项** ⇒ `13` 的 D4「少而准」是**硬收益**，不是软偏好 |
| `TIME (strict granularity) ... Do not answer a gold at a different time unit` | 判分要求**时间粒度完全一致**（DAY↔DAY），且**相对/绝对时间不可互换** → **结构化时禁止归一化原文时间表达**，必须保留字面串（这是对 D1 的一个重要修正） |
| `If memories conflict, prefer the most recent supported memory.` | 冲突消解**由平台 Answer 侧完成** → 你不必删旧证据，但**必须保留时间戳**（`created_at` 与正文时间），否则平台无法判新旧 |
| `Memories for {{speaker_1_name}}:` / `{{speaker_2_name}}:` | Answer 提示词按**说话人分组**喂记忆 → 在 `content` 文本里**显式写出说话人/实体名**更安全 |
| `Use only the provided memories. Prefer the memory that answers the question most directly.` | 直接性 > 完整性 → 排序目标与 `11` 的"证据优先序"一致 |

> 这是**唯一一手**说明"平台怎么消费你的 `data[]`"的资料，价值高于任何二手解读。**建议把 6 个 `pipeline.py` 全部读完**（`beam` 19 KB 最大、`longmemeval-s`、`personamem`、`clbench`、`scriptmem`）。

### P1-8 存在**未公开的评测模式** `light` / `scriptmem`

- 文档只写 smoke + full；但 Evaluation 页面 `Mode` 下拉实为：**`full` / `light` / `smoke` / `scriptmem`**。
- `scriptmem` 同时是 `data/` 下的一个基准目录名（ScriptMem 数据集）——该模式可能是"跑平台参考系统"。
- **这个问题的价值极高**：若 `light` **不消耗** Full 配额，就等于给了"免费的正式预跑"；若消耗，则必须避开。
- 该怎么做：**立刻问官方**（见 §5 第 2 问），并在未确认前**不要手点**。

### P1-9 Smoke 的额度算术：30 次 / 13 天，别拿它当调试器

- ✅ `每小时 1 次；本届每赛道最多 30 次`。10-18 → 10-31 共 312 小时，**限制来自 30 次**，不是每小时。
- 含义：平均 **≤2.3 次/天**，且 smoke 只验"链路能通"，不告诉你"分高不高"。
- 该怎么做：本地契约校验器覆盖全部 11 条接口约束（含中文/英文错误码、超长 `top_k`、非法条目、`user_id` 隔离对抗、Base64 图片、30 MiB 边界），**smoke 只用来确认公网可达与鉴权**。

### P2-10 一致性杂项（会咬人，但好处理）

| 项 | 情况 | 处理 |
|---|---|---|
| 域名 | `agentmemories.ai` 与 `agentmemoryleaderboard.ai` **同一站点、内容相同** | 引用时统一写 `agentmemoryleaderboard.ai`；`agentmemories.ai` 可作为备链，**不要**把任一当成仿冒 |
| 英文奖金 | 中文 ¥150,000 ↔ 英文 "US$22,000" | 以中文为准；材料里写人民币 |
| 文本规模 | 赛事页 `6,000+ instances / 32 sources` ↔ README `over 10 datasets / ~5,000 questions` | 两套计数不同（instance ≠ question），**不要混用**；写材料时引用赛事页口径 |
| 代码规模 | 赛事页 `300 评测实例` ↔ README `150 base tasks + 1,290 time-constrained tasks` | 同上，不参赛可忽略 |
| Cycle 1 榜单标签 | 首页 Preview 的 Cycle 1 就有 `Textual / Multimodal / Coding` 三个标签 | 若一期确有多模态成绩，**去榜单页扒对手基线**（页面 JS 渲染，需浏览器） |
| 组队 | 官网明确 `参赛免费，不设组队要求` | 无人数上限 |
| 202 通道 | 需绑定含 `{task_id}` 的 Add 状态查询地址，但 404 条目又称"现行同步规范不包含 Add Status 查询" | **不要用 202**，走同步 200 |

---

## 3. 你的文档里**缺失**的（建议按此更新）

| 缺失项 | 应更新到 | 严重度 |
|---|---|---|
| Media limits（10 MiB/图、30 MiB/Add、30 MiB/Search）与 Base64 强制 | `05` §一 契约表、`06` §二 材料核对 | 🔴 会直接设计错 schema |
| Answer 117,760 token 窗口 + 前缀截断 | `05` §三、`11` §7、`13` D4 | 🔴 决定返回条数与图片配额 |
| Add 幂等（`request_id`）+ 32 次重试上限 | `05` §二、"写后可见"旁 | 🔴 不去重会污染召回 |
| 429 退避 / Retry-After ≤60s / 申报并发语义 | `05` §一、`06` §2.4（第 7 项"容量"） | 🟠 影响 Full 能否跑完 |
| 开源榜 Add 侧 `gpt-4o-mini`（第二处证据） | `04` §五、`07` §一（可降级为"部分解决"） | 🟠 影响合规叙述 |
| 官方仓库与 `pipeline.py` 契约 | `02`（新增条目）、`13` §1 | 🟠 最佳一手参考 |
| `light` / `scriptmem` 模式 | `06` §五 配额表（加"未确认"行） | 🟠 可能省下一次 Full |
| 8 项 Full 前置勾选框（含 `substantive submission`） | `06` §二 核对表 | 🟡 逐项准备，避免临时卡住 |
| "不设组队要求"、免费、平台承担 Answer/Eval 成本 | `06` §一 | 🟡 材料叙述 |
| ContentPart[] 逐字段 schema（`type`/`text`/`image_url.url`） | `05` §一 | 🟡 直接照抄即可 |

---

## 4. 官网**也没有**给的（信息缺口）

| # | 缺口 | 影响 | 备注 |
|---|---|---|---|
| 1 | **多模态 7 个数据源只公开 2 个** | 决定"优化哪类题"；5 个未知来源无法本地复现 | 文本赛道公开了 6 个契约，多模态**一个都没有** |
| 2 | 多模态三组 / 14 子能力的**题目占比与权重** | 决定 D6/D7 投入 | 官网只有能力名，无分布 |
| 3 | 多模态的**评分口径**（ATM-Bench "问题质量等指标" 具体是什么；Mem-Gallery 的 F1 是答案级还是集合级） | 直接决定 D4 最优 K | 论文里 F1 是答案级；AML 措辞含糊 |
| 4 | 统一 Answer 模型的**身份**（`api_config.py` 只读环境变量，未泄露） | 决定"图该不该返回" | 若为 `gpt-4o-mini`，图可能只烧 token |
| 5 | Judge 模型与 `JUDGE_VERSION` 的具体值 | 影响可复现性论证 | 同上 |
| 6 | `light` 模式是否消耗 Full 配额 | 可能是"免费预跑" | 🆕 最高性价比的问题 |
| 7 | 多模态 embedding 若受 FAQ 05 约束，该如何选 | 多模态唯一结构性合规风险 | `07` 原问题，仍未答 |
| 8 | 多模态"图片证据合格"的判定标准 | 决定 `content[]` 怎么组 | 公告有要求，无标准 |
| 9 | 公开子集是否有下载入口 | 影响本地预检 | 只说 `公开子集用于预检与复现` |
| 10 | Full 的**排队时长** | 决定最晚启动日 | 只说"0.5–2 天 + 排队" |

---

## 5. 48 小时内该问官方的 6 个问题（更新版，按性价比排序）

1. **`light` 模式是什么？是否计入"每赛道 2 次 Full"配额？**（若不计，价值等同一次免费考试）
2. **多模态 7 个数据源分别是哪些？是否为现行公开版本或改编版？**（决定本地复现保真度）
3. **Mem-Gallery 的 F1 是答案级还是检索集合级？返回条数少于 `top_k` 是否影响评分？**（决定 D4 的 K）
4. **统一 Answer 模型是否具备视觉能力？返回 `image_url` 证据是否真的被使用？**（决定 D3）
5. **FAQ 05 的 `text-embedding-v4` 约束是否适用于开源方法榜的多模态赛道？图像编码模型如何规定？**
6. **多模态赛道是否也有类似文本赛道的公开评测契约（`pipeline.py`）？**（若有，等于免费拿到判分规则）

> 邮件：`contactus@agentmemoryleaderboard.ai`。若 48 小时内无回复，按 `13` 的推荐执行——**D1/D4/D2 在两种口径下都更优**，不依赖这些答案。

---

## 6. 对 `13` 号方案的三处修正

| `13` 的原判断 | 新证据 | 修正 |
|---|---|---|
| D4「自适应证据预算」是"最确定且免费"的增益 | P0-2（117,760 token 前缀截断）+ P0-3（30 MiB）+ 判分"多答即错" | **从"更优"升级为"必须"**，并加两条硬断言：`前 k 条图像数 ≤ 6`、`估算 token ≤ 110k`、`响应字节 ≤ 28 MiB` |
| D1「结构化记忆卡」照 SGM 走 | 判分要求时间粒度严格、相对/绝对不可互换（§4） | **结构化字段只能"附加"，不能替代原文**：`time_raw` 逐字保留 + `t_event/t_record` 派生；`raw_ref` 必填 |
| D5「跨模态校准」已降级为"绕行" | 无变化，但新增一条：返回图片必须 Base64 内联且受 30 MiB 限制 | 进一步确认"文本为主、图为辅"是对的——**图是昂贵且受限的证据形态** |
| —（新增） | P0-4 幂等 | 在 `13` 的 P0 清单里**新增"写入幂等层"**，与"吞吐与降级"并列 |

### 由此得到的「返回侧预算公式」（可直接写进代码）

```
budget = min(
  top_k,                                     # 官方上限 100
  110_000 / est_item_tokens,                 # Answer 输入 117,760，留 7k 余量
  28 MiB / est_item_bytes,                   # Search 响应 30 MiB，留 2 MiB 余量
  evidence_need(query),                      # 证据数估计：单证据 1–2 / 多证据 4–8
  len(candidates_above_threshold)            # 置信度门槛
)
```

其中 `est_item_tokens ≈ 50 + 2,500 × 图像数`，`est_item_bytes ≈ 0.7 MB × 图像数`。
→ 纯文本路径下 `budget = evidence_need ≈ 1–10`；带图路径下 `budget` 进一步被压到个位数。**"返回 100 条"在任何路径下都不成立。**

---

## 7. Evaluation Access 表单（登录后可见）：两条隐藏硬约束

> 来源：用户实拍的 `Evaluation access` 表单。⚠️ 该文案**在公开 HTML 中不存在**（`spa.txt` 全文 grep 无命中），属登录态渲染，**无法用 curl 复核**；以下按用户提供的原文记录。

### 7.1 榜单只有两档，且命名对齐了

表单 `Leaderboard track` 仅两项：

| 表单选项 | 说明原文 |
|---|---|
| **Industry leaderboard** | `For deployed products, platforms, and production systems.` |
| **Academic leaderboard** | `For research prototypes, papers, and academic systems.` |

> `The selected track is fixed to this key; every approved result will appear only on that leaderboard.`

→ **三套命名到此闭环**：`学术榜`（FAQ 05）＝ `Academic leaderboard`（表单）＝ **`开源方法榜`**（赛事页）；同理 `工业榜` ＝ `Industry` ＝ `商业产品榜`。`07` §一的 P0 冲突**实质解决**（情形 A 成立）。
→ 但**选错不可改**：所属榜单绑定在该 Key 上，所有结果只出现在该榜。要在申请前就定死 = **开源方法榜**。

### 7.2 模型约束比 FAQ 05 更硬、范围更宽 🔴

原文：

> `For non-industry submissions, any model used during Add or Search must be gpt-4o-mini. We may reproduce the evaluation; if the reproduced score differs materially, the leaderboard result may be invalidated.`

| 对比 | Full 勾选项（公开） | FAQ 05（公开） | **Evaluation 表单（登录后）** |
|---|---|---|---|
| 语气 | `expected to` | `须使用` | **`must`** |
| 范围 | `during Add` | `LLM 相关组件` | **`Add or Search`** |
| 豁免 | — | `Embedding → text-embedding-v4`；`Reranker 不作限制` | 未重述 |

**解读（按"具名豁免 + 兜底锁定"）**：

| 组件 | 是否受限 | 依据 |
|---|---|---|
| 写入侧抽取 / 摘要 / caption | 🔒 **只能 `gpt-4o-mini`** | 表单 + FAQ 05 |
| 查询改写、LLM-as-reranker、拒答判定 | 🔒 **只能 `gpt-4o-mini`** | 表单的 `or Search`（**新增约束**） |
| 专用 reranker（cross-encoder） | ✅ 自由 | FAQ 05 `Reranker 不作限制` |
| 文本 embedding | 🔒 `text-embedding-v4` | FAQ 05 |
| **图像/多模态编码** | ❓ **仍未定义** | 表单未提，FAQ 未提 |
| OCR | ✅ 自由（非 LLM） | 无条款 |

**三个直接后果**：

1. **`13` 的 D7（拒答判定）不能上大模型**：充分性判定只能用规则或 `gpt-4o-mini` → 阈值设计要更"工程化"而非"模型化"。
2. **LLM-as-reranker 也被锁**：想做重排只能用**专用 reranker**（Qwen3-Reranker / bge-reranker），不能用 `gpt-4o-mini` 之外的模型打分。
3. **多模态的最保守读法 → 全文本化**：既然图像编码模型无定义、而"Add/Search 任何模型"都受限，**只依赖 `gpt-4o-mini` 生成 caption/结构化文本 + `text-embedding-v4` 做向量**，是唯一**必然合规**的构造。
   → 而这正好是 `13` 里 E3（图像向量不如文本）推出的最优解：**合规约束与性能证据在这里收敛**。

### 7.3 复现条款：**不确定性 = 取消资格风险** 🔴

> `We may reproduce the evaluation; if the reproduced score differs materially, the leaderboard result may be invalidated.`

这条把 `11` §3.5 的"确定性"从工程美德升级为**准入条件**：

| 风险源 | 后果 | 对策 |
|---|---|---|
| LLM 抽取温度 > 0 / 版本漂移 | 重跑得到不同索引 → 分数漂移 | `temperature=0` + pin 模型版本 + **按内容哈希缓存解析结果** |
| 并列分数无全序 tie-break | 排序不稳定 → 截断点移动 → 证据集变化 | 全序 tie-break（score→时间→id 字典序） |
| 多线程/字典序依赖合并 | 同输入不同输出 | 显式排序所有聚合 |
| 缓存随 TTL 过期 | 复现时命中不同路径 | 缓存**不设过期**，或复现期内冻结 |
| 版本冻结后改代码 | 直接违规 | 部署后只允许改配置，不允许改代码 |

> 妙处：**哈希缓存同时解决"成本"与"确定性"两件事**——`13` 的级联重排 + 缓存设计在这里获得第二重理由。

### 7.4 表单必填项（材料清单的"真名单"）

| 字段 | 必填 | 备注 |
|---|---|---|
| Work email / Name / Organization-Team | ✅ | |
| Invitation code（`Inviter's LDBD Key` 后 8 位） | 可选 | 推荐码机制，作用未公开 ❓ |
| System name / Version name | ✅ | 版本名即冻结标识 |
| Add API URL / Search API URL | ✅ | 公网可达 |
| Authentication（默认 `Authorization: Token`） | ✅ | 也支持 Bearer / X-Api-Key |
| **Memory System Key** | ✅ | 平台调你接口用；静止加密，不入邮件 |
| **Public GitHub repository URL** | ✅ **表单强制** | 注意：规则说商业榜无需开源，但表单仍标必填 → 开源榜本就要，无冲突 |
| **Submission notes and run instructions** | ✅ | 原文要求同时披露：`the public repository, original authors, technical report, and every method change` |

→ 与 `06` §2.4 的 15 项核对表相比，**表单只收 10 项**，其余（声明文件、决策日志等）走仓库与 notes。**材料重心在 GitHub 仓库 + notes 的完整度**。

---

## 8. Provide Benchmarks 表单：一个被低估的免费杠杆

表单字段（用户实拍）：`LDBD key`（`ldbd_...`，仅用于organization关联，不得写入数据集）→ Benchmark title / Primary track / Estimated scale / What should this benchmark measure → 样本内容 / 上传包 / **License** / 来源论文或仓库 → 权利与署名声明。

- **许可证下拉只给**：`CC BY 4.0`、`CC BY-SA 4.0`、`Apache-2.0`、`Custom research license`、`Other / pending review`。→ 若提交，**数据用 CC BY 4.0、代码用 Apache-2.0** 最稳。
- **与赛事的关系**：赛事页 OPEN CALL 原文欢迎 `来源明确、难度适当、具有补充价值且可复核的 Benchmark`，且明令 `不含私有评测轨迹`、`不含凭据与商业内部信息`。
- **对我们的价值**：`13` 的 D10（本地代理评测集）如果做得规范，**可同时充当"基准贡献"材料**——这是唯一一条能把"自测工具"变成"公开贡献"的路径，且 `最佳技术创新奖` 的叙述可以直接引用。
- **风险（必须自我约束）**：① 绝不使用 AML 评测数据（官方 ✅ 禁止训练/重建/传播）；② 提交的是**我们自己构造的代理集**或公开基准的适配版，且必须署名原作者；③ 存在利益冲突观感 → **只在主榜提交完成后再做**，不要占用 13 天内的关键路径。
- **成本**：半天（`13` 的 D9 材料层顺带产出）。**不建议为它单独投入超过 0.5 天。**

---

## 9. 一句话

> 官网这次披露的不是"更多宣传"，而是**五条硬墙**：**只有 1 次 Full、Answer 只装 117,760 token 且截断尾部、图片必须内联 Base64 且响应 ≤30 MiB、Add 要按 request_id 幂等、以及开源榜 Add/Search 两侧模型都被锁死为 `gpt-4o-mini` 且可被复现复核**。
> 五条墙全部指向同一个设计：**少返回、强排序、文本优先、写入去重、结果确定**——与 `13` 的推荐方向同向，但把"应该"变成了"必须"，并给出了可以直接写进代码的数字。
> 而真正的空白仍是那 5 个未公开的多模态数据源、`light` 模式、以及**图像编码模型到底受不受约束**——**这三件事，一封邮件就能问清，值得今天发。**
