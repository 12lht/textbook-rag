# Agent Workspace

一个基于 LangGraph、DeepSeek、Tavily 和高德地图 API 的 Agent 学习项目。

当前项目同时提供：

- FastAPI + 网页前端
- CLI 调试入口
- 天气查询和 IP 定位
- Tavily 网页搜索与正文提取
- 多轮工具调用
- SQLite 会话持久化
- 会话列表、历史消息和 SSE 流式响应

## 项目结构

```text
agent_core.py              Agent 编排：LangGraph 图、工具、模型、消息状态读写
conversations.py           会话元数据：id、标题、重命名标记（纯应用层，不依赖 Agent）
service.py                 应用层：把上面两层拼成用例（列表/新建/重命名/删除/回退/对话流）
api.py                     FastAPI 后端
cli.py                     CLI 调试入口
frontend/index.html        网页前端
data/                      SQLite checkpoint 和会话元数据 JSON
raw/                        开发历史归档
learning-notes.md          学习笔记
requirements.txt           固定版本依赖
```

### 三层怎么分

依赖方向是单向的：`api.py` / `cli.py` → `service.py` → `conversations.py` + `agent_core.py`。

- **`conversations.py`（会话元数据）**：只读写 `data/` 下的 JSON，**不 import 任何 Agent 相关的东西**。换成任何应用都是这一套。
- **`agent_core.py`（Agent 编排）**：图、工具、模型，以及读写会话「消息状态」的操作（`get_history` / `rewind_thread` / `stream_agent`）。它**不 import `conversations`**，所以标题策略、第几轮起标题这类业务判断不在这里。
- **`service.py`（应用层）**：跨层组合集中在这里，不散落到路由和命令行里。典型例子：「删除会话」要同时删 LangGraph 的 checkpoint 和元数据 JSON；「会话列表」要同时读标题和消息条数；「回退后会话被清空」要把标题还原成默认值。

判断一个新功能该放哪：**碰不碰 LangGraph 的状态？** 碰 → `agent_core.py`；只碰业务数据 → `conversations.py`；需要两边一起动 → `service.py`。

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

- `get_weather_for_location`：通过高德查询实时天气或未来预报
- `get_user_location`：通过高德 IP 定位获取省市
- `get_current_time`：获取当前时间
- `web_search`：通过 Tavily 搜索网页
- `web_extract`：通过 Tavily 提取网页正文

## 开发说明

`agent_core.py` 是 CLI 和 Web 共用的 Agent 核心，不要在 `api.py` 或 `cli.py` 中复制工具实现。网页通过后端访问 Agent，API Key 始终留在后端环境变量中。会话相关的功能请加到 `service.py`，不要直接写进路由，否则路径和 CLI 会各写一遍跨层组合。

旧版单文件 CLI 结构保存在 `raw/3.py`，仅用于学习和对照。
