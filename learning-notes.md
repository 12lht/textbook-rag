# Agent 学习笔记

> 项目：LangGraph + DeepSeek + 高德天气 + Tavily

## 1. Python 装饰器

```python
@decorator
def function():
    pass
```

等价于：

```python
def function():
    pass

function = decorator(function)
```

`@tool` 会把普通 Python 函数转换成 LangChain 工具。函数名、参数类型和 Docstring 会暴露给 LLM。

`@dataclass` 会自动生成 `__init__` 等方法，适合定义运行时上下文。

## 2. LangChain 工具

工具负责执行确定性的程序逻辑，LLM 负责决定是否调用工具。

```text
用户问题 -> LLM 决策 -> Tool -> 工具结果 -> LLM 总结
```

工具 Docstring 应说明：

- 工具的用途
- 参数含义
- 什么时候使用
- 返回结果

系统提示词负责全局行为和工具之间的协作规则，不要重复描述工具参数细节。

## 3. Agent 与 LangGraph

当前 Agent 的主要流程：

```text
truncate -> model -> tools -> truncate -> model
                  |
                  -> END
```

- `model`：调用 DeepSeek，并决定是否调用工具
- `tools`：执行工具
- `truncate`：在进入 model 前清理旧消息
- `END`：模型生成最终回答后结束

LangGraph 的优势是显式管理状态、节点和边，适合多工具、多轮调用；简单的一问一答可以直接使用模型官方 SDK。

## 4. add_messages 与消息截断

```python
messages: Annotated[Sequence[BaseMessage], add_messages]
```

`add_messages` 是 reducer，不是普通赋值。节点返回普通消息列表时会按消息 ID 合并，不能用来直接删除旧消息。

删除消息应使用：

```python
RemoveMessage(id=message.id)
```

截断不能简单按数量切片，否则可能删除：

```text
AIMessage(tool_calls)
```

却保留对应的：

```text
ToolMessage
```

这会导致模型报错：`ToolMessage` 没有前置的 `tool_calls`。当前实现以 `HumanMessage` 为安全边界，删除边界之前的完整旧消息块。目标保留最近 100 条非系统消息，但为保证工具调用完整，实际可能超过 100 条。

## 5. thread_id 与会话

`thread_id` 是会话 ID：

- 相同 ID：读取同一段对话历史
- 不同 ID：创建独立会话
- `user_id`：用户身份
- `thread_id`：聊天窗口

当前交互规则：

- 启动时不自动进入会话
- `/switch`：展示会话摘要并按编号切换
- `/restart`：创建新会话
- 直接输入问题：没有活动会话时自动创建

## 6. 本地持久化

当前使用 SQLite checkpoint：

```text
data/agent_checkpoints.sqlite
```

会话摘要保存在：

```text
data/conversation_summaries.json
```

SQLite 保存完整 LangGraph 状态，JSON 保存用户可读的会话摘要。程序重新启动后，可以通过 `/switch` 恢复历史会话；如果启动时传入已有 `thread_id`，也可以自动显示历史。

## 7. 天气和定位工具

高德天气接口需要城市 `adcode`：

1. 城市名先调用地理编码接口
2. 得到 `adcode`
3. 使用 `extensions=base` 查询实时天气
4. 使用 `extensions=all` 查询天气预报

天气工具通过 `Literal` 暴露模式：

```python
mode: Literal["current", "forecast"]
```

高德 IP 定位只能得到公网 IP 对应的大致省市，不等于 GPS 精确定位。

## 8. Tavily 搜索

当前使用官方 `tavily-python` SDK：

- `web_search`：搜索网页，返回标题、摘要和 URL
- `web_extract`：打开 URL，提取正文

典型流程：

```text
web_search -> 选择相关 URL -> web_extract -> 阅读正文 -> 回答
```

搜索结果只是摘要。网页如果通过 JavaScript 动态加载内容，Extract 可能拿不到隐藏正文，需要浏览器自动化或专用接口。

## 9. CLI 与 Web 架构

当前 `cli.py` 是 CLI 调试入口，Agent 核心已经独立：

```text
input -> Agent -> print
```

如果做前端，建议拆成：

```text
agent_core.py  模型、工具、LangGraph
storage.py     会话和持久化
api.py         FastAPI 接口
cli.py         CLI 调试入口
frontend/      网页前端
```

网页版本通常使用 FastAPI + SSE/WebSocket 流式返回。API Key 必须留在后端。

## 10. LangChain 的取舍

LangChain/LangGraph 不是完全黑盒，状态、工具、节点、Reducer 和 Checkpoint 都可以控制，但抽象层较多。

适合使用 LangGraph 的场景：

- 多工具调用
- 工具循环
- 会话状态
- 搜索和网页提取
- 复杂 Agent 工作流

简单的一问一答或固定流程可以直接调用模型官方 SDK，减少依赖和抽象开销。

优化时重点关注：

- 模型 API 延迟
- 搜索 API 延迟
- 工具调用次数
- 上下文长度
- 超时和重试
- 日志与节点耗时

## 待学习

- FastAPI 接入现有 LangGraph
- SSE 流式返回
- 前端会话列表和消息渲染
- Agent 最大工具调用次数
- 节点耗时与错误日志
- PostgreSQL 等生产级 Checkpoint

## 11. Web 应用拆分

当前项目已经拆成：

```text
agent_core.py  Agent、工具、LangGraph 和持久化
api.py         FastAPI 接口和 SSE 流式聊天
cli.py         CLI 调试入口
frontend/      网页界面
data/          SQLite checkpoint 和会话摘要
```

网页前端不接触 API Key，只通过后端接口调用 Agent。`/api/chat/stream` 使用 SSE 返回工具状态和助手消息；`/api/conversations` 管理会话列表；`/api/conversations/{thread_id}/messages` 读取历史。
