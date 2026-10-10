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

## 12. MCP：把工具搬到另一个进程

MCP（Model Context Protocol）是 Anthropic 在 2024 年 11 月提出的开放协议，消息格式是 JSON-RPC 2.0，有两种传输方式：

- `stdio`：MCP server 作为子进程，标准输入输出就是协议通道
- `Streamable HTTP / SSE`：MCP server 跑在远端

一个 MCP server 可以暴露三类东西：tools、resources、prompts。当前项目只用了 tools。

MCP 对「工具」这个概念的理解：**MCP 的工具仍然叫 tool**。模型看到的还是 name、description 和 JSON Schema，和进程内 `@tool` 给它的信息没有区别。

「tool 和 agent 在一个进程内，MCP 把 tool 放到另外一个进程」这个理解是对的。但还有一层更关键的差别是**动态发现**：

```text
进程内工具：代码里 import 进来，写死
MCP 工具：启动时通过 tools/list 问出来，运行期才知道有哪些
```

代价是每次调用要多一次序列化和一次 IPC（或网络）往返，所以它**通常不是性能优化，反而是性能损失**。换来的是解耦、复用（一个 server 可以给多个客户端用）、独立部署和独立权限。不要指望靠它提速。

当前项目的拆分：

```text
tools.py            5 个工具的纯实现，零 langchain/langgraph 依赖
mcp_server.py       FastMCP，以 stdio 暴露同一个工具列表
agent_core.py       MCP 客户端，持有常驻会话
```

`tools.py` 里不许出现 langchain / langgraph 的 import，只能是标准库加第三方 SDK。因为它要被 MCP server 复用，加框架依赖等于把两个进程重新绑死，拆分的意义就没了。

## 13. MCP 落地时的三个坑

**一、环境变量不会自动继承。** MCP SDK 在 `env=None` 时只给子进程传一个最小环境，`.env` 里的 API Key 会全部丢失，工具静默返回「服务未配置」。必须显式传：

```python
"env": dict(os.environ)
```

**二、描述就是 docstring，而且是整个 docstring。** FastMCP 会把函数的完整 docstring 当作工具描述发给模型。如果给工具写了补充说明，要写在 docstring **外面**，否则描述会突然膨胀（实测某个工具从 33 字符变成 273 字符），改动前后的对比实验也就不公平了。

**三、anyio 的 cancel scope 有任务亲和性。** MCP 的 stdio 会话内部用 anyio 的 cancel scope，而 anyio 要求「进入和退出必须在同一个任务里」。如果按常规写法把会话挂到 `AsyncExitStack` 上跨请求复用，第一次请求正常，第二次就会抛：

```text
RuntimeError: Attempted to exit cancel scope in a different task than it was entered in
```

原因是会话在第一个请求的任务里进入，却在 lifespan 任务里退出。解法是让一个**专属的常驻 asyncio 任务**持有会话，用两个 Event 做就绪和停止信号，这样进入和退出永远在同一个任务里。

## 14. 同步图与异步 MCP：为什么要全面改 async

LangGraph 的 `graph.stream()` 是同步的，而官方 MCP SDK 的 `stdio_client`、`ClientSession`、`call_tool` 全都是 async。LangChain 有一条硬规则：**只实现了 `_arun` 的工具，在同步图里调 `.invoke()` 会直接抛错**。

所以只有两条路：给每个 MCP 工具包一层同步适配，或者把整条图改成 async。

改成 async 之后有个连锁反应，是最容易踩的地方：

```text
graph.astream()  要求异步 checkpointer
      ↓
同步的 SqliteSaver 直接抛 NotImplementedError
      ↓
换成 AsyncSqliteSaver，而它只有异步方法
      ↓
get_state / update_state  →  aget_state / aupdate_state
      ↓
往上传染到 service / api / cli / eval 一整层
```

教训：选「全面异步」时最容易漏掉的不是工具调用那一段，而是**持久化层**。LangGraph 的同步 / 异步 checkpointer 是硬绑定的，选错了会往上传染一整层。

## 15. 异步拆分的判据

哪些函数该写 async、哪些保持同步，标准只有一条：**底层要不要 await**。不要为了「风格统一」而给纯计算加 async。

| 层 | 是否 async | 原因 |
| --- | --- | --- |
| `agent_core.py` | 全 async | 碰 LangGraph 状态和 MCP |
| `service.py` | 混合 | 调 agent_core 的是 async，纯 JSON 文件操作是 sync |
| `api.py` | 混合 | 端点是不是 async，取决于它 await 什么 |

另外两点：

- FastAPI 里 `def` 端点跑在线程池，`async def` 端点跑在事件循环上。所以纯文件 IO 用 `def` 反而更合适，不会阻塞事件循环。当前 `POST /api/conversations`、`PATCH` 就故意保持 `def`。
- 异步资源要记得回收，用 `lifespan` 串起来：`lifespan` → `service.shutdown()` → `agent_core.shutdown()`，收掉 MCP 会话和 checkpointer。

## 16. 评测：怎么测出（以及怎么测不出）一个架构改动的成本

这是这一轮最有价值的教训。

工具层改完 MCP 后跑同一套 32 个用例，总 token 从 82,875 涨到 163,024（+97%）。看上去这就是 MCP 的代价。但这个读法是错的：

- **反证**：有一个用例在改后调用工具**更少**，token 反而**少了 4,068**。如果 MCP 是原因，不可能出现负值。
- **调用结构完全相同、token 差 3~4 倍**：两个用例前后都是 2 次模型调用、2 个工具结果，输入 token 却从 5,132 变成 19,360。差异只能来自搜索 API 返回的网页正文长度。
- **控制实验**：同一个查询直接调用两次波动 1.11 倍，经 MCP 调用两次波动 2.49 倍。也就是说，**网页正文的随机波动远大于 MCP 包装的开销**（包装实测只有 243~295 字符，是 content-block 结构加一个每次新生成的 uuid）。

根因是评测里混进了两个**不可控变量**：模型在同一轮里重复调用同一个工具的次数，以及外部 API 返回内容的长度。这两个量的波动都远大于被测的改动，把信号完全淹没了。

指标口径也要跟着改：**TTFT（首 token 时间）量不到工具边界**。首 token 只经过第一次模型调用，工具那一段发生在它之后。所以「TTFT 只涨了 49ms」不能当成 MCP 的成本——那是噪声。

要测清楚这个边界，得换一个**确定性工具**做微基准：输出长度固定、不发网络请求、不调模型。把边界单独计时：

| 路径 | 中位耗时 |
| --- | --- |
| 直接进程内调用 | 0.006 ms |
| langchain 适配器 → MCP（agent 实际路径） | 2.836 ms |
| 原始 `session.call_tool`（纯 JSON-RPC + 序列化） | 2.035 ms |

结论：MCP 一跳净开销约 2.8 ms，其中纯 IPC 下限 2.0 ms，langchain 包装再加 0.8 ms。子进程冷启动 1.8 s，但只发生一次（会话常驻复用）。

一般化的三条原则：

- **一次只改一个变量**。凡是有外部 API 参与的用例，要录制回放或缓存结果，否则前后不可比。
- **单次采样下，指标动 ±1 个用例属于采样噪声**，不是回归。要看趋势就把重复次数提到 3~5。
- **token 数可以免费测**：`AIMessage.usage_metadata` 里已经有确切的 input / output token，读一次状态就能拿到，不需要额外发请求。但必须在删除会话之前读。

## 17. 流式事件的重复上报

`graph.astream(stream_mode=["messages", "updates"])` 同时订阅两个通道时，同一件事可能从两个通道各来一次。如果两个通道用**不同的标识**去重（一个通道用 `call_index` 整数，另一个用 `tool_call["id"]` 字符串），去重的集合永远匹配不上，就会重复上报。

**根因是标识不统一，不是去重逻辑没写。** 与其加一层映射把两种标识对齐，不如让某一类事件只由一个通道负责——把 `tool_call` 的上报从 `messages` 通道整体移除，问题就消失了。

这也提醒：发现自己要写「把 A 格式的键转成 B 格式的键才能比」时，先想想是不是上游本来就不该产生两种格式。

## 18. 顺带修掉的一个静默失败

`cli.py` 里一直在等 `event["type"] == "assistant"` 这个事件，但流水线里**从来没有产生过它**（实际发的是 `assistant_delta`）。结果是 CLI 能正常调用模型、能正常出工具状态，但回答内容一个字都不打印。

这种 bug 的特征是「没报错，但功能像没写」。所以前端和 CLI 的错误都必须显示在可见的位置，静默失败最容易被误判成「功能没做」。

## 待学习

- **Memory 摘要化**：现在 `truncate_node` 超出上限是直接丢弃旧消息，要改成结构化摘要，并测长对话的事实准确率变化
- **向量库**：接入本地向量库，做长期记忆检索和本地文档 RAG，产出 Recall@k 和 MRR
- **录制回放搜索 API**：让搜索类用例的评测前后可比（见第 16 节）
- **可观测性**：先自建 trace 表记节点耗时和错误，再挂 LangSmith
- **verifier 节点**：拿草稿回答和原始工具结果交叉核对（明确不做 supervisor 多智能体）
- **Agent 最大工具调用次数**：模型会在同一轮重复调用同一个工具，需要一个 guard
- **PostgreSQL 等生产级 Checkpoint**
