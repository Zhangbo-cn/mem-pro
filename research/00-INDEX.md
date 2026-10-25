# 00 · 索引与方法

- 生成时间：2026-10-14T01:47:48+08:00
- 执行方：电脑端 pi Agent `polished-swan`（device `admin`，Agent ID `tc-grkdhdvc`）
- 请求方：手机端 Agent `lunar-storm`（device `localhost`，Agent ID `tc-zi0nnbd8`），Conversation `cv_46ecnkmxts6gp`
- 用户决策（已给定）：**参赛组别 = 开源方法榜**；**赛道待定**（本文档给出推荐）

## 文件清单

| 文件 | 内容 |
|---|---|
| `00-INDEX.md` | 本文件：索引、调研方法、来源清单、置信度标注规则 |
| `01-track-differences.md` | **重点**：三赛道 10 维度差异对比 + 决策矩阵 + 推荐赛道 |
| `02-papers.md` | 论文（27 篇，arXiv ID 全部经 API 核验） |
| `03-projects.md` | 开源项目/框架（16 个，星级/license/最近提交经 GitHub API 实测） |
| `04-models.md` | 模型选型（Embedding / Reranker / LLM / 多模态与 OCR） |
| `05-tech-stack.md` | 技术栈与架构（MVP + 冲榜架构 + 数据流 + 接口契约 + 成本量级） |
| `06-open-method-track-checklist.md` | 开源方法榜参赛清单与合规要求 |
| `07-open-questions.md` | 未证实项、口径冲突、需澄清问题 |
| `13-multimodal-direction-candidates.md` | **多模态可做方向评估**（基准论文已定位、7 条消融证据、方向打分与推荐组合、13 天路线、市场方向） |
| `15-ann-index-selection.md` | **向量索引（ANN）选型**：官方规模实测（真实 v4 向量）、索引类型对比、不上的理由与触发阈值 |
| `urlcheck.txt` | 53 条 URL 的实际 HTTP 校验原始结果 |
| `gh_raw.txt` | 16 个 GitHub 仓库的 API 原始返回 |
| `arxiv_raw*.txt` | arXiv API 原始返回（论文标题/ID/日期/作者） |

## 置信度标注规则

| 标记 | 含义 |
|---|---|
| ✅ **已核实** | 有一手来源，且经实际 HTTP/API 校验通过（附状态码或 API 实测字段） |
| ⚠️ **部分核实** | 来源可靠但为二手，或目标站点不可直连、只能间接核验 |
| ❓ **未核实 / 存疑** | 无法访问、无法确认，或存在口径冲突；需用户或官方澄清 |

## 调研方法

1. **一手抓取**：`curl -sS -m 25 -L` 抓取 AML 官网并剥离标签取纯文本（见 `mem-pro/记忆之巅QA全文.md` 的核验记录）。
2. **GitHub 元数据**：`api.github.com/repos/{owner}/{repo}` 实测 `stargazers_count` / `license.spdx_id` / `pushed_at` / `archived`，写入 `gh_raw.txt`。
3. **论文核验**：`export.arxiv.org/api/query?search_query=ti:"<标题>"`，取回真实 `<id>` / `<published>` / 作者；**不使用记忆或推测的 arXiv 编号**。
4. **URL 连通性**：对 53 条引用 URL 执行 `curl -o /dev/null -w "%{http_code}"`，结果写入 `urlcheck.txt`。
5. **模型事实**：web 检索 + 官方文档页面（阿里云百炼、OpenAI）。

## 网络环境限制（重要，影响置信度）

本机对以下站点的**直连实测结果**（见 `urlcheck.txt`）：

| 站点 | 结果 | 影响 |
|---|---|---|
| `api.github.com` | ✅ 200 | 仓库星级/license/最近提交均为**实测值** |
| `raw.githubusercontent.com` | ✅ 200 | 可读仓库文件 |
| `export.arxiv.org` | ✅ 200 | 论文 ID 均为**实测值** |
| `arxiv.org/abs/*` | ✅ 全部 200（27/27） | 论文链接可直达 |
| `agentmemoryleaderboard.ai` | ✅ 200 | 官方口径一手来源 |
| `github.com`（网页） | ❌ 000 超时 | **网页不可达**，故一律用 API 核验；仓库网页链接仅作引用，未做网页级校验 |
| `huggingface.co` | ❌ 000 超时（IPv4/IPv6 均超时） | 模型页**无法直连**，相关条目降级为 ⚠️/❓ |
| `openai.com/index/...` | ⚠️ 403 | 有反爬；改用 `developers.openai.com` 等间接来源 |

> 结论：**论文与 GitHub 仓库是硬核实测的**；HuggingFace 模型页与部分博客属间接来源，已在各文档中逐条标注。

## 来源清单（一手来源）

1. https://agentmemoryleaderboard.ai/competition —— 赛事页 + FAQ 9 条（抓取时间 2026-10-14T01:23+08:00）
2. https://agentmemoryleaderboard.ai/rules —— 参赛说明 + Add/Search 接入规范（同上；实测与 `/`、`/evaluation`、`/api-guide` 同为 222090 B 同一 SPA 外壳）
3. https://api.github.com/repos/{owner}/{repo} —— 16 个仓库元数据（2026-10-14T01:35+08:00 前后）
4. https://export.arxiv.org/api/query —— 论文元数据（2026-10-14T01:38—01:44+08:00）
5. https://help.aliyun.com/zh/model-studio/embedding —— `text-embedding-v4` 规格
6. https://developers.openai.com/api/docs/models/gpt-4o-mini —— `gpt-4o-mini` 规格

## 一句话结论

**推荐赛道：文本记忆（Textual）**，备选多模态记忆（Multimodal）。理由与量化矩阵见 `01-track-differences.md`。

> 📌 **后续更新**：用户已拍板**多模态**；且 2026-10-18 已定位 `ATM-Bench`（arXiv 2603.01990）与 `Mem-Gallery`（arXiv 2601.03515）的公开论文与 MIT 代码，故**「本地替代基准少、自测信号弱」的判断对多模态已不成立**——详见 `13-multimodal-direction-candidates.md` §1。
