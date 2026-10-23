# 测试与探针

本项目的验证脚本。**不是 pytest 套件**——没有 conftest、没有 fixture 机制，
每个脚本自己就是一次实验：跑一遍、打印数据、人看结论。

脚本原先散在 `.workbuddy/tmp/`（190 个条目混着截图、日志、测试 PDF），
现已按用途归到这里，一次性产物（截图 / 日志 / 被取代的迭代版）已删除。

## 目录

| 目录 | 内容 |
|---|---|
| `probes/` | 后端探针，Python，直接 import 项目模块 |
| `e2e/` | 前端端到端，Node + playwright-core，跑真实浏览器 |
| `bench/` | 系统核心指标基准：token 消耗、缓存命中、时延（定义见 `bench/README.md`） |
| `fixtures/` | 测试素材（双栏 PDF 等） |

## 怎么跑

所有命令都在**仓库根目录**执行。脚本里的 `REPO_ROOT` 由 `__file__` 反推
（`tests/probes/x.py` → `parents[2]`，`eval/x.py` → `parents[1]`），
所以不管仓库放在哪个盘、叫什么名字都能跑，不用改任何路径。

```bash
# Python
python tests/probes/test_rewind.py

# Node（playwright-core 需要能解析到，可以用 NODE_PATH 指过去）
NODE_PATH=<你的 node_modules 目录> node tests/e2e/e2e-cite-popover.js
```

e2e 脚本的浏览器路径由 `_browser.js` 自己找：先读环境变量 `CHROME_PATH`，没有就在
playwright 的浏览器缓存目录里挑版本最高的 chromium。换机器不用改脚本。

**要读教材 PDF 的探针**（解析、切分、检索那一批）在脚本头部的 `CASES` / `PDF` 常量里
写的是本机路径，跑之前改成你自己的书；`tests/fixtures/` 里只有一份 `twocol.pdf`，
够跑双栏排版那几个。

跑 e2e 前先起后端（`uvicorn api:app`），脚本里的 `BASE` 指向对应端口。

## 命名约定

| 前缀 | 含义 |
|---|---|
| `probe-` | 一次实验，打印数据供人判断，不设断言 |
| `test-` | 回归测试，断言失败即报错，可反复跑 |
| `e2e-` | 真实浏览器 + 真实 uvicorn（部分会调真模型） |
| `audit-` | 批量体检，产出可量化的报告 |

`probe_rewind.py` 是历史遗留的下划线命名，内容与 `probe-*` 同类。

## 成本标记

「调模型」列：`是` = 会真实花掉 API 配额；`否` = 零模型调用，随便跑。
「碰生产库」列：`否` = 用临时库 / 临时目录，不会污染 `data/`。

---

## 一、回归测试

三个 `test-*` 是带断言的，失败即报错。**2026-10-22 全部重新跑通**
（`test_rename` / `test-api-async` 之前因为 `create_conversation` 加 `user_id`
而失效，且没人发现——接口一改脚本就静默烂掉，见下面「维护提醒」）。

| 脚本 | 用途 | 调模型 | 碰生产库 |
|---|---|---|---|
| `test_rewind.py` | 回退：`service.rewind_conversation` 完整路径 + FastAPI 接口 | 否 | 是（finally 清理） |
| `test_rename.py` | 重命名会话的接口回归 + 「手动命名不被自动标题覆盖」 | 是 | 是（finally 清理） |
| `test-api-async.py` | 会话全生命周期 + SSE + 404 分支（20 项） | 是（1 次短问答） | 是（finally 清理） |
| `test-guest-isolation.py` | 访客隔离：身份签发 / 验签 / 会话互不可见 / 访客池 / 过期清理（19 项） | 否 | **否**（全程临时目录） |
| `probe_rewind.py` | `update_state` + `RemoveMessage` 截断 thread 消息 | 否 | 是 |

### 维护提醒

改 `service.py` 的函数签名后，**必须回来跑一遍这三个 `test-*`**。
它们没有任何自动触发机制（没有 CI），签名漂移只会让它们在下次手动跑时报
`TypeError`——而那时可能已经隔了几周。`test-guest-isolation.py` 是唯一不碰
生产库的，改身份相关的东西时优先跑它。

## 二、端到端全链路

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `e2e-layering.py` | 拆层之后走完整链路：真 uvicorn + 真模型 | 是 |
| `e2e-real-api.py` | 对真 FastAPI 后端做端到端回退（prompt 极短） | 是 |
| `probe-citations-e2e.py` | 跑一轮真实教材问答，验证 citations 事件流通 | 是 |
| `probe-e2e-rag.py` | 真教材 → 真检索工具 → 真模型回答 | 是 |
| `probe-chapter-tasks.py` | 章节提纲 / 出题 / 对照问答三题实测：模型检索几次、覆盖哪些章、引用是否跨书 | 是 |
| `probe-toggles-e2e.py` | 深度思考 / 联网搜索两个开关，看事件流变化 | 是 |
| `probe-title-trigger-e2e.py` | `chat_events` 在第几轮才推 title 事件 | 是 |

## 三、解析与切分（PDF → Markdown → chunk）

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-agents-full.py` | Hello-Agents 整本（720 页）跑一遍：耗时、段数、图数 | 否 |
| `probe-algo-code.py` | 在 Hello-Algo（LaTeX 排版）上验证同一套规则 | 否 |
| `probe-documents.py` | 教材摄取（M1）在真实 PDF 上的表现 | 否 |
| `probe-formats.py` | PDF / PPTX / DOCX 三种格式都能走完摄取 | 否 |
| `probe-preflight.py` | 解析前预检 + warning 列 + 上传上限 | 否 |
| `probe-producer.py` | PDF 产地：Producer / Creator / 页面尺寸 / 每页字数 | 否 |
| `probe-markdown.py` | Markdown 中间态长什么样 | 否 |
| `probe-parse-after.py` | 改动后的实际产出：段长分布、代码块数、样例 | 否 |
| `audit-ingest.py` | 三本基线书重跑解析 + 切分，输出可量化脏点 | 否 |
| `audit-md.py` | 审计 `data/` 里已 ready 书的解析成品质量 | 否 |

### 段落结构判定

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-blockstruct.py` | block 结构指纹：一行是不是一个 block | 否 |
| `probe-gap.py` | 真实行距：段内 vs 段间，验阈值 0.75 倍字号 | 否 |
| `probe-split-reason.py` | 几何规则为什么该合没合（逐对打印三个判据） | 否 |
| `probe-side-by-side.py` | 按 block 聚合 vs 按几何合并，谁更接近原文段落 | 否 |
| `probe-paragraph.py` | 模拟按几何重建段落，不碰 `documents.py` | 否 |
| `diff-split-gap.py` | 间隙 2.0 → 0.35 前后，Markdown 哪些行变了 | 否 |

### 代码块识别

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-code-font.py` | 代码能不能靠字体 / 颜色识别（逐行字体名） | 否 |
| `probe-mono.py` | 等宽字体分布是不是可靠信号 | 否 |
| `probe-mono-x0.py` | 代码块左边缘与正文左边缘差多少 | 否 |
| `probe-code-final.py` | 最终判据：mono 主导行 + 缩进 + 0.4 字号 | 否 |
| `probe-false-code.py` | 被误判成代码的正文行长什么样 | 否 |

### 上下标与插图

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-superscript.py` | 定位上下标的真实 bbox / 字号 | 否 |
| `probe-superscript-all.py` | 全本跑 `_line_text`，列出所有还原出的上标 | 否 |
| `probe-page-region.py` | 打印某页某 y 区间的所有 line | 否 |
| `probe-script-shape.py` | 扫描「小字号 + 基线偏移」片段，评估误伤面 | 否 |
| `probe-frame-filter.py` | 排版框不许把插图吞掉（回归） | 否 |
| `probe-frame-origin.py` | 矢量矩形里有多少被判定成排版框 | 否 |
| `probe-ppt-layout.py` | PPT 页的 block 结构与图形覆盖面积 | 否 |
| `probe-ppt-figure.py` | 逐页量：文本量、矢量占比、位图数量 | 否 |
| `reparse-slides.py` | 重解析全部幻灯片版式的书（让修复落到已入库的书） | 否 |

## 四、检索与评测

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-retrieval.py` | BM25 单用 vs BM25 + 向量（真书入库、临时库） | 否 |
| `probe-hybrid-ab.py` | 同上，严格判据版 | 否 |
| `probe-fusion3.py` | 四种检索模式同题对照：BM25 / 向量 / RRF / 加权融合 | 否 |
| `probe-judge.py` | 模型当裁判，逐条判「这段能不能回答这个问题」 | 是 |
| `probe-rerank.py` | 候选池 → bge-reranker-v2-m3 重排 → 裁判打分 | 是 |
| `probe-chunksize.py` | TARGET_CHARS 500 vs 250 对照 | 否 |
| `probe-short-chunks.py` | 短正文 chunk 到底是什么 | 否 |
| `probe-prod-verify.py` | 生产改动落地：TARGET=250 + rerank + 邻居扩展 | 否 |
| `probe-citations.py` | 引用溯源后端：解析函数、`get_history` 重建 | 否 |
| `probe-asset-render.py` | Markdown 图引用能不能真渲染（`?render=1`） | 否 |
| `probe-weknora.py` | WeKnora 默认参数搬到本项目语料上跑 | 待确认 |
| `probe-search-variance.py` | web_search 结果长度波动拆成「MCP 包装」与「网页内容」 | 是 |
| `eval-baseline-off.py` | 关掉上标还原后跑评测，重建改动前基线 | 是 |

## 五、Agent / 工具 / 流式

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-agent-core-async.py` | MCP + 全量异步改造验证 | 否 |
| `probe-stream-node-attribution.py` | `stream_mode="messages"` 的 token 是哪个节点说的 | 否 |
| `probe-reasoning-chunks.py` | 思维链藏在流式分片的哪里 | 否 |
| `probe-reasoning-e2e.py` | 思维链从模型到前端的完整链路 | 是 |
| `probe-toolcall-events.py` | tool_call 事件重复上报是否修好 | 否 |
| `probe-tool-status.py` | `get_history` 有没有把工具成败带出来 | 否 |
| `probe-tool-schema.py` | 工具 schema 与 `learning-notes` 的说法是否一致 | 否 |
| `probe-toolruntime.py` | 进程内工具 + `ToolRuntime[Context]` 注入可用性 | 否 |
| `probe-tool-result-size.py` | MCP 是否把工具结果包装变长（追 token 翻倍） | 否 |
| `probe-mcp-server.py` | `mcp_server.py` 的工具名 / schema / 描述 | 否 |
| `probe-mcp-overhead.py` | 把 MCP 边界开销从模型行为里剥离 | 否 |
| `probe-toggles.py` | 两个开关真的改变了发给模型的东西 | 否 |
| `probe-title-model.py` | 起标题用的是不是主模型同一个 provider | 否 |
| `probe-title-schedule.py` | 起标题的节奏（第几轮重起） | 否 |
| `probe-usage.py` | 能不能从 checkpoint 读到真实 token 用量 | 否 |
| `probe-cassette.py` | 录制回放四条路径：record / replay / 未命中 / off | 否 |
| `inspect-thread-tools.py` | 只读：某会话每次工具调用的参数与返回原文 | 否 |
| `lesson-tool.py` | 教学：普通函数 vs `@tool` 差在哪 | 否 |

## 六、记忆与摘要

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-memories.py` | 记忆存储层（`memories.py`）行为 | 否 |
| `probe-memory-tools.py` | 「模型自己决定什么时候记」的工具集 | 否 |
| `probe-memory-scale.py` | 存储在什么规模上开始疼 | 否 |
| `probe-memory-service.py` | 走 `service.chat_events` 看记忆是否落盘 | 是 |
| `probe-memory-live.py` | 真跑几轮，看模型会不会主动 `save_memory` | 是 |
| `probe-memory-context.py` | 反复需要记忆时模型会不会重复读 | 是 |
| `probe-memory-panel.py` | 记忆面板存储层：列表 / 改 / 删，含「改完编号不变」回归 | 否 |
| `probe-memory-api.py` | 记忆面板 HTTP 层：401 / 404 / 409 / 跨账号隔离 | 否 |
| `probe-memory-migrate.py` | 老记忆库补 `id` 列：不丢字段、幂等 | 否 |
| `probe-summary.py` | 摘要裁剪与累积逻辑，判据是 token 估算（零成本） | 否 |
| `probe-summary-live.py` | 真模型跑 `summarize_history`，看早期事实有没有留住 | 是 |
| `probe-summary-e2e.py` | 真实图里跑一遍摘要化 | 是 |
| `probe-summary-echo.py` | 摘要会不会被模型原样复述（对照） | 是 |
| `probe-token-estimate.py` | 标定 token 估算误差；断言工具结果没被转义成 `\u5faa` 发出去 | 是 |

## 七、账号、会话、位置

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `probe-users.py` | 账号与登录存储层（`users.py`） | 否 |
| `probe-auth-api.py` | 认证与越权隔离的 HTTP 层 | 否 |
| `probe-invite.py` | 邀请制：首人免码、码一次性、重名不烧码 | 否 |
| `probe-invite-api.py` | 邀请码的 HTTP 层：400 / 401 / 403 分得清 | 否 |
| `probe-limits.py` | 配额与限流：边界、跨天、退还、假时钟推时间 | 否 |
| `probe-limits-api.py` | 429 + `Retry-After`，以及「额度没了就不跑模型」 | 否 |
| `probe-conversation-owners.py` | 会话归属（`conversations.py`） | 否 |
| `probe-client-location.py` | 客户端位置这条链路 | 是 |

## 八、基础设施与微基准

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `bench/bench.py` | 系统核心指标基准（token / 缓存命中率 / TTFT / 时延分位），口径见 `bench/README.md` | **是** |
| `probes/probe-usage-shape.py` | usage_metadata 字段勘察：provider 到底回传哪些 token 字段 | 是（2 次短调用） |
| `probe-gil-contention.py` | CPU 密集线程会不会拖慢事件循环 | 否 |
| `probe-sqlite-journal-modes.py` | 各种日志模式下一次提交的耗时 | 否 |

## 九、工具类脚本

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `ingest-textbooks.py` | 把 `textbook/` 的 26 个 PDF 走生产链路入库 | 否 |
| `export-agents-md.py` | Hello-Agents 整本导出成一份可直接打开的 Markdown | 否 |
| `export-sample-md.py` | 导出一段含图的 Markdown，肉眼验收解析质量 | 否 |
| `make-render-preview.py` | 解析 → Markdown → 渲染，导出成自包含 HTML 快照 | 否 |
| `recount-forecast02.py` | 把 `allow_tools` 改动套用到历史基线上重算 | 否 |

## 十、前端 e2e

| 脚本 | 用途 | 调模型 |
|---|---|---|
| `e2e-layout.js` | 布局：侧栏 / 输入框 / 滚动位置 | 否 |
| `e2e-scroll.js` | 滚动行为 | 否 |
| `e2e-scrollbutton.js` | 回到底部按钮 | 否 |
| `e2e-sidebar.js` | 侧栏折叠 | 否 |
| `e2e-sidebar-wheel.js` | 侧栏滚轮 | 否 |
| `e2e-wheel.js` | 滚轮事件 | 否 |
| `e2e-rewind.js` | 回退交互 | 否 |
| `e2e-rewind-error.js` | 回退失败时的错误提示 | 否 |
| `e2e-conversation-menu.js` | 会话右键菜单 | 否 |
| `e2e-favicon.js` | favicon 加载 | 否 |
| `e2e-cite-popover.js` | 引用角标小窗（mock 接口 + 真实页面） | 否 |
| `_frontend_check.js` | 前端整体自检 | 否 |
| `mock-server.js` | mock 后端（e2e 依赖） | 否 |
| `mock-conversations.js` | mock 会话数据 | 否 |
| `mock-rewind.js` | mock 回退接口 | 否 |
| `probe-frontend-citations.mjs` | 前端引用渲染（linkedom 真 DOM） | 否 |
| `probe-frontend-library.mjs` | 前端资料库渲染 | 否 |
| `probe-frontend-reasoning.mjs` | 前端思维链渲染 | 否 |
| `probe-frontend-toggles.mjs` | 前端开关状态 | 否 |
| `probe-frontend-memory.mjs` | 前端记忆面板：入口显隐、改删、传的是 id 不是原文 | 否 |
| `probe-frontend-invite.mjs` | 前端邀请码：注册框何时出现、码列表、只有主人看得到 | 否 |

---

## 维护约定

1. **一次实验只留一个脚本**。同一问题迭代到 v6 时，删掉 v1~v5——原来的
   `probe-code-split` ~ `v6` 六个版本平铺在 tmp 里，谁也分不清哪个是现行的。
2. 新探针直接建在 `probes/`，**不要再往 `.workbuddy/tmp/` 丢**。tmp 是临时区，
   会被清空。
3. 输出只打印到 stdout。截图、日志、导出的 PDF/Markdown 用完就删——它们是
   验证过程的副产物，不是资产。
4. **路径不写死**：仓库内的路径一律用 `REPO_ROOT / "..."`，截图这类临时产物写
   `os.tmpdir()`。写死盘符的脚本换台机器就废，而它不会报错，只会静默跑错目录。
