# Agent Workspace

一个基于 LangGraph、DeepSeek、Tavily 和高德地图 API 的 Agent 学习项目。

当前项目同时提供：

- FastAPI + 网页前端
- CLI 调试入口
- 账号注册 / 登录，会话按用户隔离
- 未登录访客按浏览器分配签名身份：彼此看不见、7 天后清理、配额有全局上限
- 工具通过 MCP 协议跑在独立进程（可独立部署、可复用），需要请求上下文的工具留在进程内
- 天气查询，以及由用户授权提供的客户端定位
- Tavily 网页搜索与正文提取
- 多轮工具调用
- SQLite 会话持久化
- 会话列表、历史消息和 SSE 流式响应
- 量化评测闭环：金标集 + 指标基线 + 工具录制回放

## 项目结构

```text
tools.py                   4 个「纯函数」工具的实现（零框架依赖，只有标准库 + tavily）
mcp_server.py              MCP server：用 stdio 把 tools.py 暴露给 Agent
geocode.py                 客户端坐标 → 可读位置（高德逆地理编码，带缓存）
agent_core.py              Agent 编排：LangGraph 图、模型、MCP 客户端、3 个进程内工具（定位 + 记忆读写）、消息状态读写
users.py                   账号与登录：账号表、密码哈希、登录令牌、邀请码（纯应用层，不依赖 Agent）
conversations.py           会话元数据：id、标题、重命名标记、会话归属（纯应用层，不依赖 Agent）
memories.py                跨会话长期记忆：一条事实一行存进 SQLite，去重、清洗、上限（纯应用层，不依赖 Agent）
limits.py                  配额与限流：按天记用量（SQLite）+ 内存滑窗防刷（纯应用层，不依赖 Agent）
service.py                 应用层：把上面几层拼成用例（身份收敛/会话用例/回退/对话流/位置收敛/注入记忆后端）
api.py                     FastAPI 后端
cli.py                     CLI 调试入口
cassette.py                评测用的工具录制回放
eval/cases.jsonl           评测金标集（32 个用例）
eval/run.py                评测 runner
eval/baselines/            历史基线 JSON
eval/cassettes/            录制下来的工具返回值
frontend/index.html        网页前端（单文件，内联脚本，无构建步骤）
frontend/library.html      知识库管理页
frontend/vendor/           marked + DOMPurify（本地化，不再依赖 CDN）
data/                      SQLite（checkpoint / 长期记忆 / 账号 / 教材）和会话元数据 JSON
scripts/backup.py          备份脚本：走 SQLite 在线备份接口，不是 cp
Dockerfile                 运行镜像（--workers 1 是硬约束，理由在文件里）
docker-compose.yml         编排：应用容器 + 数据卷
raw/                       开发历史归档
learning-notes.md          学习笔记
requirements.txt           固定版本依赖
```

### 三层怎么分

依赖方向是单向的：`api.py` / `cli.py` → `service.py` → `conversations.py` + `users.py` + `agent_core.py`。

- **`conversations.py` / `users.py` / `memories.py`（业务数据）**：只读写 `data/` 下的文件，**不 import 任何 Agent 相关的东西**。换成任何应用都是这一套。
- **`agent_core.py`（Agent 编排）**：图、工具、模型，以及读写会话「消息状态」的操作（`get_history` / `rewind_thread` / `stream_agent`）。它**不 import `conversations`**，所以标题策略、第几轮起标题这类业务判断不在这里。
- **`service.py`（应用层）**：跨层组合集中在这里，不散落到路由和命令行里。典型例子：「删除会话」要同时删 LangGraph 的 checkpoint 和元数据 JSON；「会话列表」要同时读标题和消息条数；「回退后会话被清空」要把标题还原成默认值；「你是谁」要从令牌解析出来再决定这次能不能用长期记忆。

判断一个新功能该放哪：**碰不碰 LangGraph 的状态？** 碰 → `agent_core.py`；只碰业务数据 → `conversations.py` / `users.py`；需要两边一起动 → `service.py`。

## 文档

| 文件 | 讲什么 |
| --- | --- |
| `README.md` | 这个文件：是什么、怎么跑、代码放在哪 |
| `docs/ARCHITECTURE.md` | 为什么这么设计：MCP 拆分、账号与令牌、持久化、长期记忆、工具分界 |
| `docs/API.md` | 接口清单、请求示例、SSE 流式事件 |
| `docs/EVAL.md` | 评测口径、金标集、录制回放、怎么读基线对比 |
| `docs/PROJECT-PLAN.md` | 教材助教的功能架构（做什么 / 不做什么 / 验收标准） |
| `docs/DEPLOY.md` | 怎么部署到公网：Docker、反向代理、HTTPS、备份、上线检查清单 |
| `learning-notes.md` | 学习笔记，按编号章节续写（34 章 + 待学习清单） |
| `tests/README.md` | 测试与探针的索引：每个脚本干什么、要不要调模型 |

## 环境准备

所有命令都在**项目根目录**（这个 README 所在的目录）执行，先切过去：

```powershell
cd <你 clone 下来的目录>
```

使用 Python 3.11 或更高版本，并安装依赖（下面的 `python` 指你装好依赖的那个解释器）：

```powershell
python -m pip install -r requirements.txt
```

在项目根目录创建 `.env`，配置 API Key：

```env
DEEPSEEK_API_KEY=你的DeepSeek_API_Key
AMAP_API_KEY=你的高德地图_API_Key
TAVILY_API_KEY=你的Tavily_API_Key
MODEL_PROVIDER=deepseek
```

不要把真实 Key 提交到代码仓库或发送给前端。

需要使用 GLM-4.7-Flash 时，将模型开关改为：

```env
MODEL_PROVIDER=glm
```

此时程序会使用 `GLM_API_KEY` 和智谱 OpenAI 兼容接口；不设置或填写其他值时继续使用 DeepSeek。

**主模型和起标题用同一个 provider**，`MODEL_PROVIDER` 一处说了算。标题模型只是温度调低（0.2）、并关掉推理：

| 用途 | 温度 | max_tokens | 推理 | 说明 |
| --- | --- | --- | --- | --- |
| 主对话 | 0.5 | 5000 | 开 | 要智能 |
| 起标题 | 0.2 | 400 | **关** | 要稳定和短，不需要思考过程 |

关推理是必须的，不是优化：`deepseek-flash` 是**推理模型**，回答前先输出一段思维链，那部分也算 `output_token`。原来沿用 GLM 时代的 `max_tokens=40`，结果 40 个 token 全被思考吃掉、正文变成空串（`finish_reason: length`），表现就是「标题莫名其妙没了」。关掉之后实测输出从 40 token 降到 **4**。

`max_tokens=400` 是**安全垫不是预期用量**——万一日后换模型、关推理的参数被静默忽略，还有余地出标题。上限宽着不花钱，`max_tokens` 是天花板不是目标。

`GLM_API_KEY` 留着做备用是可以的，但它**不再影响起标题**：标题走不走智谱只由 `MODEL_PROVIDER` 决定。

## 启动网页应用

```powershell
python -m uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

不想 `cd` 也可以把项目目录直接告诉 uvicorn（`.` 就是当前目录）：

```powershell
python -m uvicorn api:app --app-dir . --host 127.0.0.1 --port 8000 --reload
```

浏览器打开：

```text
http://127.0.0.1:8000
```

API 文档：

```text
http://127.0.0.1:8000/docs
```

> 改了 `api.py` 或 `agent_core.py` 之后必须重启服务（加上 `--reload` 会自动重载）。
> 不加 `--reload` 时，旧进程仍然会从磁盘读取新的 `index.html`，所以会出现
> 「页面是新按钮、但接口报错」这种现象——前端能更新，Python 代码不能。
>
> 判断方法看状态码：**404** 说明整条路径都没注册，**405** 说明路径在、只是这个方法没注册
> （例如 `DELETE /api/conversations/{id}` 生效、`PATCH` 报 405，就是进程还停在加 PATCH 之前）。
> 都可以打开 `http://127.0.0.1:8000/docs` 核对实际注册了哪些方法，重启后即可恢复。

**要给同学用（公网访问）不要直接把 `--host` 改成 `0.0.0.0`**：那是裸 HTTP，
令牌明文过网，而且没有反代就关不掉 SSE 缓冲、也收不到真实的 `X-Forwarded-For`
（访客限流会全部算到同一个 IP 上）。走容器 + 反向代理，步骤见 `docs/DEPLOY.md`。

## 启动 CLI

```powershell
python cli.py
```

CLI 命令：

```text
/switch     展示会话摘要并选择历史会话
/restart    新建会话
q           退出程序
```

直接输入普通消息时，如果当前没有活动会话，程序会自动创建新会话。

CLI **不走登录**：它是本机单人使用，直接用 `service.CLI_IDENTITY`（默认账号 + **开着**记忆）。网页侧的访客身份是另一套：每个浏览器领一个签名 cookie（`g-xxxxxxxx`）当独立身份，记忆是关的。两者 `user_id` 不同（CLI 用默认账号 `"1"`，访客用 `g-` 前缀），所以互不干扰。这也是为什么记忆开关是逐请求的、而不是绑在身份上。

## 开发说明

`agent_core.py` 是 CLI 和 Web 共用的 Agent 核心，不要在 `api.py` 或 `cli.py` 中复制工具实现。网页通过后端访问 Agent，API Key 始终留在后端环境变量中。会话相关的功能请加到 `service.py`，不要直接写进路由，否则路径和 CLI 会各写一遍跨层组合。

加工具或改工具的注意事项：

- 改工具的函数签名或 docstring 会直接改变模型看到的接口，动手前先想清楚是不是必要。
- **加工具分两种，先判断它需不需要本次请求的上下文**：
  - 返回值只由参数决定的「纯函数」→ 写进 `tools.py` 并加进 `ALL_TOOLS`，`mcp_server.py` 会自动注册；
  - 需要请求上下文的（比如当前位置）→ 留在 `agent_core.py`，写 `@tool` + `runtime: ToolRuntime[Context]`。
- **两种情况都要登记进 `agent_core._TOOL_ORDER`**，否则合并后工具会被排到末尾，发给模型的顺序就变了。
- 参数里的 `runtime` / `ToolRuntime` 不会出现在模型看到的 schema 里（被框架剥离），但工具描述（docstring）**会**原样发出去。
- MCP 工具跑在子进程里，它拿到的环境变量是**会话建立时**从父进程继承的快照。改环境变量后要让会话重开（`agent_core.shutdown()` 会把 MCP 客户端作废）才会生效。
- 在 async 链路里做阻塞 IO（`urllib`、`requests`）必须用 `asyncio.to_thread` 包起来，否则会卡住整个事件循环——包括正在流式输出的 SSE。`geocode.describe` 就是这样的调用。

启动网页应用时会在后台拉起一个 MCP server 子进程，这是正常现象；`Ctrl+C` 时 FastAPI 的 lifespan 会把它一起收掉。如果命令行看起来「卡住不退出」，通常是子进程没被回收。

旧版单文件 CLI 结构保存在 `raw/3.py`，仅用于学习和对照。

