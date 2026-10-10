# Agent Workspace

一个基于 LangGraph、DeepSeek、Tavily 和高德地图 API 的 Agent 学习项目。

当前项目同时提供：

- FastAPI + 网页前端
- CLI 调试入口
- 工具通过 MCP 协议跑在独立进程（可独立部署、可复用）
- 天气查询和 IP 定位
- Tavily 网页搜索与正文提取
- 多轮工具调用
- SQLite 会话持久化
- 会话列表、历史消息和 SSE 流式响应
- 量化评测闭环：金标集 + 指标基线 + 工具录制回放

## 项目结构

```text
tools.py                   5 个工具的真正实现（零框架依赖，只有标准库 + tavily）
mcp_server.py              MCP server：用 stdio 把 tools.py 暴露给 Agent
agent_core.py              Agent 编排：LangGraph 图、模型、MCP 客户端、消息状态读写
conversations.py           会话元数据：id、标题、重命名标记（纯应用层，不依赖 Agent）
service.py                 应用层：把上面两层拼成用例（列表/新建/重命名/删除/回退/对话流）
api.py                     FastAPI 后端
cli.py                     CLI 调试入口
cassette.py                评测用的工具录制回放
eval/cases.jsonl           评测金标集（32 个用例）
eval/run.py                评测 runner
eval/baselines/            历史基线 JSON
eval/cassettes/            录制下来的工具返回值
frontend/index.html        网页前端
data/                      SQLite checkpoint 和会话元数据 JSON
raw/                       开发历史归档
learning-notes.md          学习笔记
requirements.txt           固定版本依赖
```

### 三层怎么分

依赖方向是单向的：`api.py` / `cli.py` → `service.py` → `conversations.py` + `agent_core.py`。

- **`conversations.py`（会话元数据）**：只读写 `data/` 下的 JSON，**不 import 任何 Agent 相关的东西**。换成任何应用都是这一套。
- **`agent_core.py`（Agent 编排）**：图、工具、模型，以及读写会话「消息状态」的操作（`get_history` / `rewind_thread` / `stream_agent`）。它**不 import `conversations`**，所以标题策略、第几轮起标题这类业务判断不在这里。
- **`service.py`（应用层）**：跨层组合集中在这里，不散落到路由和命令行里。典型例子：「删除会话」要同时删 LangGraph 的 checkpoint 和元数据 JSON；「会话列表」要同时读标题和消息条数；「回退后会话被清空」要把标题还原成默认值。

判断一个新功能该放哪：**碰不碰 LangGraph 的状态？** 碰 → `agent_core.py`；只碰业务数据 → `conversations.py`；需要两边一起动 → `service.py`。

## 工具为什么跑在另一个进程里（MCP）

工具的实现不在 Agent 进程里，而是跑在一个独立的 MCP server 子进程，Agent 通过 MCP 协议（JSON-RPC over stdio）调用：

```text
tools.py  ──被加载──▶  mcp_server.py
                            ▲  JSON-RPC（stdio）
                       agent_core.py  ← 启动时通过 tools/list 动态发现工具
```

**MCP 不是提速手段。** 它多一次序列化和一次进程间往返，实测每次工具调用净增约 2.8 毫秒（子进程冷启动 1.8 秒，但只发生一次，会话是常驻复用的）。换到的是解耦：工具实现可以独立部署、独立授权，同一个 server 能被多个客户端复用。判断该不该用 MCP，看的是「这个工具要不要被别的东西复用」，不是「它快不快」。

几条硬约束：

- **`tools.py` 里不许出现 langchain / langgraph 的 import。** 它要被 MCP server 加载，加框架依赖等于把两个进程重新绑死，拆分的意义就没了。
- **每个函数的 docstring 就是模型看到的工具描述**，而且是**整个 docstring**。要写补充说明就写在 docstring 外面，否则描述会膨胀，改前改后的对比就不公平了。
- **给 MCP 子进程传环境变量必须显式 `env=dict(os.environ)`。** mcp SDK 在 `env=None` 时只传一份精简环境，`.env` 里的 API Key 全部丢失，所有工具会静默变成「服务未配置」。
- MCP 会话由 `agent_core` 在一个**专属常驻 asyncio 任务**里持有。不能挂在 `AsyncExitStack` 上跨请求复用——stdio 会话内部是 anyio 的 cancel scope，要求进入和退出在同一个任务里，否则第二次请求就会抛 `Attempted to exit cancel scope in a different task`。

改造前后可以用 git 标签对照，两版的工具接口（名称、参数、必填项、描述文本）是逐字一致的，只有 `title` 后缀差一个 langchain 加上的 `Arguments`：

```powershell
git tag                      # pre-mcp / post-mcp-async
git show pre-mcp:agent_core.py
```

同一时期整个状态层改成了异步（`graph.astream` 要求异步 checkpointer，`AsyncSqliteSaver` 只有异步方法，于是 `get_state` / `update_state` 一路往上变成了 `aget_state` / `aupdate_state`）。**判据只有一条：底层要不要 await。** 纯 JSON 文件操作保持同步，不要为了统一而统一。

## 环境准备

所有命令都要在项目目录下执行。如果之前在用别的地方，先切过来：

```powershell
cd D:\agent
```

使用 Python 3.11 或更高版本，并安装依赖：

```powershell
C:\Users\13526\miniconda3\envs\agent\python.exe -m pip install -r .\requirements.txt
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

## 启动网页应用

```powershell
cd D:\agent
C:\Users\13526\miniconda3\envs\agent\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000 --reload
```

不想 `cd` 也可以把项目目录直接告诉 uvicorn：

```powershell
C:\Users\13526\miniconda3\envs\agent\python.exe -m uvicorn api:app --app-dir D:\agent --host 127.0.0.1 --port 8000 --reload
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

## 启动 CLI

```powershell
cd D:\agent
C:\Users\13526\miniconda3\envs\agent\python.exe .\cli.py
```

CLI 命令：

```text
/switch     展示会话摘要并选择历史会话
/restart    新建会话
q           退出程序
```

直接输入普通消息时，如果当前没有活动会话，程序会自动创建新会话。

## API 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `GET` | `/api/conversations` | 获取会话列表 |
| `POST` | `/api/conversations` | 创建新会话 |
| `DELETE` | `/api/conversations/{thread_id}` | 删除会话 |
| `PATCH` | `/api/conversations/{thread_id}` | 重命名会话 |
| `GET` | `/api/conversations/{thread_id}/messages` | 获取历史消息 |
| `POST` | `/api/conversations/{thread_id}/rewind` | 回退到某条用户消息之前 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |

重命名请求示例：

```json
{
  "title": "天气与出行的讨论"
}
```

标题会折叠连续空白并截断到 30 字（同一规则也用于自动起标题）。手动改过名字的会话会被标记下来，后续消息不会再触发自动起标题把它覆盖掉；标题为空返回 400，会话不存在返回 404。

删除会话的入口在网页上移到了会话条目的右键菜单里，和「重命名」放在一起。

回退请求示例：

```json
{
  "turn": 1
}
```

`turn` 是用户消息的序号，从 0 开始。后端会删除该条消息以及它之后的全部消息，并把原文返回给前端放回输入框。

聊天请求示例：

```json
{
  "message": "我这里未来几天的天气怎么样？",
  "thread_id": null,
  "user_id": "1"
}
```

`thread_id` 为空时，后端会自动创建新会话。后续请求使用返回的 `thread_id` 即可继续对话。

## 数据持久化

会话数据位于 `data/`：

```text
data/agent_checkpoints.sqlite
data/conversation_summaries.json
data/conversation_renames.json
```

SQLite 保存 LangGraph 的消息状态，JSON 保存会话列表中的可读摘要。`conversation_renames.json` 记录哪些会话被手动改过名字，用来让自动起标题跳过它们。删除这些文件会清空本地会话记录。

## 工具

实现全部在 `tools.py`，注册在 `mcp_server.py`，Agent 侧通过 MCP 加载：

| 工具 | 作用 |
| --- | --- |
| `get_weather_for_location` | 通过高德查询实时天气或未来预报（`mode=current` / `forecast`） |
| `get_user_location` | 通过高德 IP 定位获取省市和 adcode |
| `get_current_time` | 获取当前时间 |
| `web_search` | 通过 Tavily 搜索网页（`topic=general` / `news`） |
| `web_extract` | 通过 Tavily 提取网页正文 |

注册顺序刻意与改造前保持一致（有些模型对工具顺序敏感）。不要在 `api.py` 或 `cli.py` 里复制工具实现。

每个工具还套了一层 `cassette.recorded` 用于评测录制回放，默认模式 `off` 时行为和没套一样。

## 评测

金标集在 `eval/cases.jsonl`，32 个用例分 8 类（天气 / 定位 / 时间 / 搜索 / 网页提取 / 多工具 / 负例 / 工具边界）。

```powershell
# 只校验用例，不调模型
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --dry-run

# 跑全量，写入 eval/baselines/<时间戳>.json
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --tag baseline

# 冒烟：只跑前 3 个 / 只跑某几个分类
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --limit 3
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --category 搜索,网页提取

# 和旧基线逐项对比
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --compare .\eval\baselines\baseline-v1.json
```

指标口径：工具选择完全匹配率、工具召回率 / 精确率、负例抑制率、工具调用成功率、端到端通过率、TTFT P50/P95、端到端 P50/P95、总 token 消耗、重复的 tool_call 事件数。token 数从 checkpoint 里的 `usage_metadata` 读，不额外发请求。

### 录制回放：让改动前后真正可比

**直接跑两轮去比是被外部内容污染的。** 搜索结果正文长度实测波动 1.1~2.5 倍，比被测的代码改动大得多，2024 年这轮改动就出现过「总 token 涨 97% 但其实和改动无关」的假信号。所以对比前先把工具输出冻结下来：

```powershell
# 1. 录一轮（真实调用外部 API，把返回值按「工具名 + 参数」存进 cassette）
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --cassette-mode record --cassette frozen-v1 --tag rec

# 2. 之后所有对比都走 replay，两次运行拿到的工具输出逐字一致
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --cassette-mode replay --cassette frozen-v1 --tag a
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --cassette-mode replay --cassette frozen-v1 --tag b --compare .\eval\baselines\a.json
```

规则：

- `record` **只增不改**，所以可以反复跑把覆盖面加宽（取并集）。
- `replay` 未命中时会照常真实调用，并计入「外部调用未命中」。**这个数不为 0 就说明有些工具的入参是新的**，那些样本的对比仍然不干净——要么再跑一轮 record 加宽，要么把未命中的样本挑出来单独看。
- 录制点在工具函数内部，所以改造前后的两版代码共用同一个 cassette 文件，A/B 才算同一个实验。
- 单次采样（`--repeat 1`）下，指标动 ±1 个用例属于模型的采样噪声，不是回归。要看趋势把 `--repeat` 提到 3~5。

## 开发说明

`agent_core.py` 是 CLI 和 Web 共用的 Agent 核心，不要在 `api.py` 或 `cli.py` 中复制工具实现。网页通过后端访问 Agent，API Key 始终留在后端环境变量中。会话相关的功能请加到 `service.py`，不要直接写进路由，否则路径和 CLI 会各写一遍跨层组合。

加工具或改工具的注意事项：

- 改 `tools.py` 的函数签名或 docstring 会直接改变模型看到的接口，动手前先想清楚是不是必要。
- 加工具只要在 `tools.py` 里写函数并加进 `ALL_TOOLS`，`mcp_server.py` 会自动注册；不要在 `agent_core.py` 里硬编码工具。
- 工具跑在子进程里，它拿到的环境变量是**会话建立时**从父进程继承的快照。改环境变量后要让会话重开（`agent_core.shutdown()` 会把 MCP 客户端作废）才会生效。

启动网页应用时会在后台拉起一个 MCP server 子进程，这是正常现象；`Ctrl+C` 时 FastAPI 的 lifespan 会把它一起收掉。如果命令行看起来「卡住不退出」，通常是子进程没被回收。

旧版单文件 CLI 结构保存在 `raw/3.py`，仅用于学习和对照。
