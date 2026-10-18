# mem-pro

按手机端 Agent 请求创建的「竞赛 QA / 资料」归档目录。

## 文件清单

| 文件 | 大小 | 行数 | 说明 |
|---|---|---|---|
| `记忆之巅QA全文.md` | 见 `ls -la` | 见下 | 线上官方材料的完整中文整理（12 个部分，原文照录） |
| `README.md` | 本文件 | — | 文件清单、来源 URL、FAQ 条数、核验结论、与 MISSING.md 的关系 |
| `MISSING.md` | 约 4.6 KB | 69 行 | 第一轮「本地检索」结论：本机不存在该竞赛素材（保留不删） |
| `research/` | 目录 | 8 份文档 | 深度调研（赛道差异/论文/项目/模型/技术栈/参赛清单/待澄清），见下 |

## research/ 目录（深度调研）

| 文件 | 内容 |
|---|---|
| `research/00-INDEX.md` | 索引、调研方法、来源清单、置信度规则、**网络环境限制** |
| `research/01-track-differences.md` | **重点**：三赛道 10 维度对比 + 加权决策矩阵 + **推荐文本赛道** |
| `research/02-papers.md` | 27 篇论文（arXiv ID 经 API 核验，27/27 HTTP 200） |
| `research/03-projects.md` | 16 个开源项目（星级/license/最近提交经 GitHub API 实测） |
| `research/04-models.md` | 模型选型 + **FAQ 05 约束是否适用于开源方法榜** 的双分支方案 |
| `research/05-tech-stack.md` | MVP + 冲榜架构、接口契约、自测策略、成本量级 |
| `research/06-open-method-track-checklist.md` | 开源方法榜材料清单、配额、红线、17 天倒排计划 |
| `research/07-open-questions.md` | 口径冲突、未核实项、**待澄清问题（含时间窗矛盾）** |
| `research/08-differentiation.md` | 差异化路径（证据编译 / 时间正确 / 规则隔离） |
| `research/09-memory-loss-and-scenario-design.md` | 失忆机制分层、场景化设计、代价矩阵 |
| `research/10-positional-effects-and-ordering.md` | 位置效应（U 型）与输出顺序、4 篇论文核验 |
| `research/11-ordering-semantics-and-generality.md` | **输出顺序的语义定义 + 通用性×针对性取舍** |
| `research/12-multimodal-differentiation.md` | **多模态赛道差异化（按官方三组能力重映射）+ 6 个多模态专属杠杆** |
| `research/13-multimodal-direction-candidates.md` | **多模态可做方向深度评估**：两个官方基准的论文/代码已定位（推翻 02/07 旧判断）+ 7 条消融证据 + 10 个方向的打分与推荐组合 + 13 天路线 + 市场方向 |
| `research/14-cycle2-site-recheck.md` | **第二期官网复核与指南交叉核对**：四条硬墙（1 次 Full / 117,760 token / 30 MiB / 幂等）、三套榜单命名映射、官方 Answer+Judge 契约、两个表单的隐藏约束与全部信息缺口 |
| `research/urlcheck.txt` | 53 条 URL 的 HTTP 校验原始结果 |
| `research/gh_raw.txt` | 16 个仓库的 GitHub API 原始返回 |
| `research/arxiv_raw*.txt` | arXiv API 原始返回 |

**📌 最终决策（2026-10-14）**：组别 = **开源方法榜**；赛道 = **多模态记忆（Multimodal）**。
量化推荐原为「文本 4.00 > 多模态 3.70 > 代码 2.95」，用户基于"应用场景更多、可做内容空间更大、价值通用性更强"选择多模态，决策已采纳。

**核验数据**：URL 校验 53 条 = 32×200 / 20×000（github.com 与 huggingface.co 本机不可直连）/ 1×403；论文 27/27 可访问；仓库 16/16 API 取到实测值。

## 来源 URL

- https://agentmemoryleaderboard.ai/competition —— 赛事页，含「参赛问答」FAQ（**9 条**）
- https://agentmemoryleaderboard.ai/rules —— 参赛说明 + Add/Search 接入指南
  - 实测 `/`、`/rules`、`/evaluation`、`/api-guide` 返回**同一 SPA 外壳**（均为 222090 字节）
  - `/competition` 为独立页面（34185 字节）
- 抓取方式：`curl -sS -m 25 -L`，随后剥离 script/style/标签取纯文本
- 抓取时间：2026-10-14T01:23:00+08:00 ｜ 抓取方：polished-swan

## FAQ 条数

**9 条**（01—09），位于 `/competition` 赛事页「参赛问答 · 常见问题」，已在文档第十一节逐条原文照录。

## 核验结论

| 核验项 | 官方原文数值 | 结论 |
|---|---|---|
| 正式评测 Top K | `正式外部评测固定为 100` | 一致 |
| Full 次数限制 | 每 AML Key 每赛道最多 2 次；第 2 次须在首次 Full 完成后 30 天发起 | 一致 |
| Smoke 配额 | 每小时 1 次；本届每赛道最多 30 次 | 一致 |
| 材料截止 | 2026-10-31 23:59（UTC+8） | 一致 |
| 评测停止 | 2026-11-04 23:59（UTC+8） | 一致 |
| 公榜 | 2026 年 11 月中旬 | 一致 |
| 奖金池 | ¥150,000（每赛道 ¥50,000） | 一致 |
| 「记忆之巅」字样 | 官网命中 **0** 次 | 该名称**非官方** |
| FAQ 05 口径 | FAQ 用「学术榜/工业榜」，赛事页用「开源方法榜/商业产品榜」 | 出入已如实记录，以官方公告为准 |

官方中文标题为「**Agent 记忆挑战赛｜忆决高下**」；英文 Agent Memory Challenge（AMC）2026；平台为 Agent Memory Leaderboard（AML）。

## 与 MISSING.md 的关系

- `MISSING.md` 记录**第一轮（本地检索）**的结论：本机不存在该竞赛的 QA/题面素材，并列出已搜范围与未覆盖区域。
- 本轮任务前提更新为「**素材在线上**」，故新增 `记忆之巅QA全文.md` 收录线上官方材料。
- **两者的结论并不矛盾**：本地确实没有；线上确实有。`MISSING.md` 按要求**保留不删**，作为第一轮排查的存档。
- 注意 `MISSING.md` 中「本地不可能有 QA 全文文件」的判断依然成立——AML 题面由平台编排下发，不以下发文件形式提供；本目录收录的是**官方公开说明与 FAQ**，不是评测题面。

## 声明

- 文档内引号内容均为官方页面**原文照录**，未概括改写。
- 未编造任何题目、答案或数字。
- `compectition` 下其他目录**未被改动**。
- 抓取过程为只读；未上传任何文件。请勿将 `Eval Key`、`Memory System Key` 等凭据写入本目录或公开仓库。
