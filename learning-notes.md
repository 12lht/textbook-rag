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

这种 bug 的特征是「没报错，但功能像没写完」。所以前端和 CLI 的错误都必须显示在可见的位置，静默失败最容易被误判成「功能没做」。

## 19. 当前架构（工具 MCP 化 + 全链路异步之后）

第 3 节画的是进程内工具时代的图，按时间线原样保留。这里记当前架构。

先说结论：**图本身没有变**。节点还是 `truncate` / `model` / `tools` 三个，边还是
`truncate → model →（有工具调用）tools → truncate`，`should_continue` 判断结束。
变的是「工具跑在哪里」以及「状态怎么读写」。

### 进程结构

```text
┌── Agent 进程 ──────────────────────────────────────────┐
│  api.py / cli.py     HTTP 路由 / 命令行 REPL            │
│  service.py          应用层用例，跨层组合都集中在这里      │
│  conversations.py    会话元数据（纯 JSON，不碰 Agent）    │
│  agent_core.py       LangGraph 图、模型、MCP 客户端       │
└────────────────────────┬──────────────────────────────┘
                         │  MCP（JSON-RPC over stdio）
┌────────────────────────┴──────────────────────────────┐
│  MCP server 子进程                                      │
│  mcp_server.py        FastMCP，注册 5 个工具             │
│  tools.py             工具的真实实现，零框架依赖          │
└────────────────────────┬──────────────────────────────┘
                         │  HTTPS
                    高德 / Tavily
```

### 一次请求的完整链路

```text
用户输入
  → api.py（SSE 端点）或 cli.py
  → service.py            读历史、判断这一轮要不要起标题
  → agent_core.stream_agent()
  → LangGraph 图

      ┌──────────┐        ┌─────────┐
      │ truncate │ ─────→ │  model  │ ──── 无 tool_calls ───→ END
      └──────────┘        └─────────┘
           ↑                   │
           │              有 tool_calls
           │                   ↓
           │              ┌─────────┐
           └──────────────│  tools  │
                          └─────────┘
                               │
                               └─ 经 MCP 跳到子进程里的 tools.py
```

延迟归属这一点容易搞错：**TTFT（首 token 时间）只覆盖「第一次模型调用」那一段**，
工具那一跳发生在它之后。所以首字延迟反映不出 MCP 的成本，MCP 的开销体现在端到端。

### 与第 3 节的逐项差异

| 维度 | 第 3 节（旧） | 当前 |
| --- | --- | --- |
| 工具实现在哪 | `agent_core.py` 里的 `@tool`，同在 Agent 进程 | `tools.py` + `mcp_server.py`，独立子进程 |
| 工具怎么被发现 | import 时写死在代码里 | 会话建立时通过 `tools/list` 动态发现 |
| 工具的接口从哪来 | langchain `@tool` 生成 | FastMCP 从 docstring + 函数签名生成 |
| 调用工具的代价 | 进程内函数调用 | 每次多约 2.8 ms，返回值多约 250 字符的 content-block 包装 |
| 状态读写 | 同步（`SqliteSaver`，`get_state` / `update_state`） | 异步（`AsyncSqliteSaver`，`aget_state` / `aupdate_state`） |
| 生命周期 | 模块级直接建连接 | `lifespan` 里建立，`shutdown()` 里回收 |

### 为什么这么拆

工具实现独立成进程之后，可以独立部署、独立授权，同一个 server 能被多个客户端复用。
但要说清楚：**它不是提速手段**，每次调用反而多一次序列化和一次进程间往返。判断该不该
用 MCP，看的是「这个工具要不要被别的东西复用」，不是「它快不快」。

顺带一提，这个拆分让工具层的语言和框架都可以换——只要它还讲 MCP，Agent 侧不用动。

具体踩坑在第 13、14 节，异步拆分的判据在第 15 节，评测口径的变化在第 16 节。

## 20. 哪些工具不能放进 MCP：判据是「需不需要本次请求的上下文」

MCP 化之后常见的一个误解，是把「工具」都当成可以搬进 MCP server 的等价物。第 12 节说过 MCP 改的是工具的**交付方式**。但有一种工具**根本搬不过去**。

### 一个真实的错误

项目里有个 `get_user_location`，回答「我在哪个城市」。它原来是这么实现的：

```python
@recorded("get_user_location")
def get_user_location() -> str:
    """通过高德 IP 定位服务获取当前用户所在的省、市和 adcode。"""
    params = urlencode({"key": api_key})
    with urlopen(f"https://restapi.amap.com/v3/ip?{params}", timeout=10) as response:
        data = json.load(response)
    return f"当前位置：{data.get('province', '')}{data.get('city', '')}，adcode：{data.get('adcode', '')}"
```

它挂在 MCP server 里，看起来没什么问题。直到把评测记录摊开，发现一个案子很怪：用户问了「**明天适合去广州出差吗**」，广州已经写在问题里，模型却还是去调了一次定位。

顺着调用序列看下去，真相是模型在做一件比预期更聪明的事：

```text
get_current_time                    → 「明天」是哪天
get_weather_for_location(广州)      → 目的地
get_user_location()                 → 「那我从哪出发？」
get_weather_for_location(北京)      → 出发地
```

它把「适不适合出差」理解成需要**两边天气**的问题。而 `get_user_location` 用 IP 定位返回了「北京市」——正好是这台机器的出口 IP 所在地。模型于是断言「**你当前在北京**」。

这里有两个各自独立的问题，第二个严重得多。

### 问题一：它猜的是「请求来源」，不是「用户」

IP 定位拿到的城市来自**请求的出口 IP**。单机自用「碰巧对」，掩盖了一个产品级缺陷：**这个服务一旦多用户部署，所有用户都会被定位到服务器所在地**。每个人都会被告知「你当前在北京」。

配额浪费是花钱，这个是**答错**——而且是把「不知道」伪装成「知道」，模型据此向用户陈述了一个它无权陈述的事实。

### 问题二：它没有合适的通道——但原因和我们想的不一样

**先说结论：不是 MCP 协议做不到，是这一层的框架没接。**

MCP 协议本身留了逐次调用的旁路字段——`CallToolRequest.params._meta`，SDK 里就是 `session.call_tool(name, args, meta={...})`。挡住它的是本项目用的 `langchain-mcp-adapters`：它的 `execute_tool` 调 `call_tool` 时不传 `meta`，interceptor 能覆盖的字段也只有 `name` / `args` / `headers`，不含 `meta`。

这个区别很重要：**「协议不允许」只能改架构，「框架没实现」只是工作量问题。**

**我在这里写错过一次结论。** 最初这节写的是「MCP 协议里只有工具名和参数，没有 per-request 通道」——去翻 SDK 的 `call_tool` 签名才发现 `meta` 参数一直都在。教训：**下「做不到」这种判断之前，要看那一层的源码，不要从上一层的行为反推。** 把「框架没实现」说成「协议不允许」，面试里一句「`_meta` 是干嘛的」就问穿了。

### 那到底能不能绕？

绕开适配器、自己写一层包装也能走通（`session.call_tool(name, args, meta={...})` 是现成的接口），代价是从此要自己维护 MCP 工具的 schema 转换、结果解包和错误处理。

所以准确的表述是：**不是做不到，是这么做不值得。**

### 更根本的：就算通道打通，也不该这么做

这一条比技术可行性重要。工具的作用是让模型**去获取它不知道的东西**。而「用户在哪」是**客户端已知的事实**——让模型先决定"我要调一个工具来问一个我已经被告诉过的信息"，本身就是反模式：

- 多一次模型往返（多一个失败点）；
- 多一次计费调用；
- 而且模型仍然可能不去调（措辞稍微变化就不调了），信息就白给了。

顺带排掉一个更深的坑：**位置也不能混进参数让模型转手**。就算后端已经拿到了坐标，把它塞进工具参数让模型当二传手，等于让模型复述一个它并不掌握的信息——这跟让模型填 `location` 参数是同一个错误。

一句话：这类数据属于**编排层的输入**，该在请求进入图的时候就放进去，而不是转成一个"工具调用"绕一圈回来。

### 判据（双向的）

> **返回值只由参数决定 → 放 MCP；需要本次请求的上下文 → 留在进程内。**

按这条判据，`get_user_location` 回到了 `agent_core.py`：

```python
@tool
def get_user_location(runtime: ToolRuntime[Context]) -> str:
    """获取用户当前所在的城市。位置由用户在客户端授权提供。"""
    context = getattr(runtime, "context", None)
    location = getattr(context, "location", None)
    if not location:
        # 把「怎么办」也告诉模型，否则它可能编一个城市出来。
        return "用户没有提供位置信息。如果回答依赖位置，请先询问用户所在的城市。"
    return f"用户授权提供的当前位置：{location}。"
```

代价是架构上出现「4 个走 MCP + 1 个走进程内」的例外。但这个例外**是有判断支撑的，不是妥协**。

**反过来也要说清楚**：如果哪个工具将来需要独立部署、跨语言实现、或者单独授权（也就是 MCP 真正的价值所在，见第 12 节），那"留在进程内"才是做错的那个。判据是双向的，不是「MCP 不好」。

### 位置这条链路

位置由客户端提供，分两种来源：

```text
浏览器 geolocation（用户授权）          用户手动选城市（拒绝授权时的兜底）
        │ 坐标                          │ 城市名
        └────────────┬─────────────────┘
                     ▼
      service.resolve_client_location      ← 优先级规则只写一处
                     │  可读文本，如「广东省广州市」
                     ▼
      agent_core.Context.location
                     │  ToolRuntime 注入
                     ▼
      get_user_location  ──▶  返回给模型
```

**优先级：手选城市 > 授权坐标。** 手选是用户明确确认过的；坐标可能来自 VPN、代理或粗粒度的 Wi-Fi 定位，可靠性反而更低。规则收敛到一个函数里，`/api/location` 端点和聊天链路共用，不会两边打架。

拿不到位置时，工具如实回答「用户没有提供位置」，**不退回任何猜测**。这是关键：让「不知道」保持为「不知道」，比猜一个填上要好。

### 三个容易踩的点

**一、高德逆地理编码的参数是「经度,纬度」。**

```python
# 浏览器给的是 {latitude, longitude}，高德要的是「经度,纬度」——顺序正好相反。
location = f"{longitude:.6f},{latitude:.6f}"
```

传反了**不报错**，只会安静地定位到地球另一边（116, 39 和 39, 116 都是"合法"坐标）。这类错误不会自己冒出来，只能在返回值上核对。

**二、直辖市的 `city` 字段是空列表，不是空字符串。**

实测同一个接口：

| 地点 | `province` | `city` | 类型 |
| --- | --- | --- | --- |
| 北京天安门 | `'北京市'` | `[]` | **list** |
| 广州塔 | `'广东省'` | `'广州市'` | str |
| 成都 | `'四川省'` | `'成都市'` | str |

北京、上海、天津、重庆的 `city` 是**空列表**。不判类型直接拼接，会得到 `北京市[]` 这种结果，或者在 f-string 里抛错。所以：

```python
def _text(value: object) -> str:
    """高德的字段可能是字符串，也可能是空列表（`city` 对直辖市就是 `[]`）。"""
    return value if isinstance(value, str) else ""
```

**三、async 里的阻塞 IO。**

坐标解析走 `urllib`，是阻塞调用，而它落在 `service` 的 async 链路里：

```python
# 直接在事件循环里跑会卡住所有并发请求，包括正在流式输出的那条 SSE。
return await asyncio.to_thread(geocode.describe, latitude, longitude)
```

「异步函数里做同步 IO」是最典型的性能陷阱：代码看起来是异步的，实际上整个事件循环被一个网络请求按住了。判据很简单——**只要你写了 `async def`，里面就不能有阻塞调用**。

### 位置为什么不写进 checkpoint

位置**不**存进会话状态，而是每次请求通过 `Context` 注入。原因：它是**逐请求的输入**，不是会话的累积事实。用户换个地方，下一轮就该变；写进 checkpoint 会变成「第一轮在哪就永远在哪」。

这条和第 17 节那个「重复上报」的教训是同一个道理：**状态该属于哪个维度（请求级 / 会话级 / 进程级），要一开始就想清楚**，放错了层级后面很难改。

### 一个附带的观察：改口径会影响历史结论

定位工具不再发外部请求之后，评测里那份「哪些工具烧外部配额」的清单必须同步更新（`METERED_TOOLS`）。

连带效应很有意思：某个用例里「多调一次定位」原本被判定为**浪费配额**，现在配额不烧了，它仍然**算违规**——因为性质是**越界**（用户没问，模型自己扩了需求），而不是浪费。也就是说，**同一条记录在不同口径下会得到不同的归因**。改口径时要把受影响的旧结论一起过一遍，否则会留下自相矛盾的记录。

## 21. 工具的参数从哪来：三个来源，和模型看到的那张「名片」

上一节说「位置该由框架给」。但真去看模型手里的工具列表会发现一个奇怪的地方：

```text
get_current_time       参数：{}（空）
get_user_location      参数：{}（空）
```

两个都是 0 个参数，看起来一样。**其实完全不是一回事**——一个是真的不需要输入，另一个是框架替它填好了。

### 三个来源

工具的参数只有三个来源：

| 来源 | 谁决定 | 例子 | 工具放哪 |
| --- | --- | --- | --- |
| 模型填 | 模型从用户的话里读出来 | `city`、`mode` | MCP |
| 框架塞 | 框架从本次请求的 `Context` 注入，模型看不见这个参数 | `runtime` → `location` | 进程内 |
| 工具自算 | 函数体内部算出来 | `datetime.now()` | MCP |

**关键：判断「要不要留在进程内」看的是值从哪来，不是参数有几个。**

`get_current_time` 也是 0 个参数，但它谁调都一样（返回值只由参数决定，只是参数恰好为空），所以它老老实实待在 MCP 里是对的。反过来，`get_user_location` 虽然是 0 个参数，但它要的是**本次请求**的位置，所以必须留在进程内。

### 模型看到的那张「名片」叫 schema

模型决定调哪个工具、填什么参数，靠的是一份**自描述**：工具名、说明、参数表。这份描述就是 schema，它由函数签名**生成**，不是手写的。

同一份签名会生成好几个名字不同的 schema，第一次见容易晕：

| 名字 | 是什么 | 谁看它 |
| --- | --- | --- |
| 函数签名 | 源码里那个 `def` | 人 |
| `args_schema` | 框架内部的原始形态 | 框架 |
| `tool_call_schema` | 剥掉注入参数之后的版本 | **模型** |
| `inputSchema` | MCP server 对外公布的 | MCP 客户端 |
| `convert_to_openai_tool()` 的输出 | 转成 OpenAI 函数调用格式 | **模型** |

写探针取 schema 时要用 **`tool_call_schema`** 或 `convert_to_openai_tool(tool)`，**别用 `args_schema`**：

- 进程内 `@tool` 给的 `args_schema` 是 pydantic 模型，MCP 适配器给的已经是 dict，两种来源形态不同，直接对比是拿苹果比橘子；
- 带 `ToolRuntime` 参数的工具，`args_schema.model_json_schema()` 会直接抛 `PydanticInvalidForJsonSchema` —— `ToolRuntime` 里有 Callable 字段，生不出 JSON Schema。

### `runtime` 是什么

`runtime: ToolRuntime[Context]` 是**本次请求的运行环境**，框架在调用工具的那一刻塞进去。它带了 `state` / `context` / `config` 等字段，本项目只用 `context` —— 就是那个装着 `user_id` 和 `location` 的 `Context`。

它**不会出现在模型看到的名片里**：框架生成 schema 时会把它剥掉（用 `convert_to_openai_tool` 验证过，`get_user_location` 发给模型的参数确实是空的），但函数的 docstring 照旧原样发出去。

所以这里没有黑魔法，就一句话：**参数分两类，一类模型填，一类框架填。**

### 改完之后，一次真实调用长什么样

问题「我这里今天的天气怎么样？」，客户端授权的位置是广州，实测调用序列：

```text
① 模型调 get_user_location            参数 = {}
② 工具返回「用户授权提供的当前位置：广东省广州市」
③ 模型调 get_weather_for_location     city = 广东省广州市, mode = current
④ 模型调 get_weather_for_location     city = 广东省广州市, mode = forecast
```

有两个地方特别容易看错：

**一、被注入的是定位工具，不是天气工具。** 框架塞的是 `get_user_location` 的 `runtime`；天气工具的 `city` 仍然是**模型自己填**的——它只是把上一步的结果抄了过去，省掉了「问用户」这一步，并不是不用填。

**二、天气工具的 `city` 必须留成模型填。** 如果把它也改成框架注入当前位置，这个工具就只会回答「这里的天气」，问「明天适合去广州出差吗」「北京和上海哪个热」就全废了。位置的含义是「**我默认关心哪里**」，不是「这个工具只能查哪里」。

顺带一个实测观察：模型填的是 `广东省广州市` 这个完整串，而不是 `广州市`。能通过是因为 `get_weather_for_location` 内部会把城市名转成 adcode，高德容忍这种带前缀的写法。

## 待学习

- **Memory 摘要化**：现在 `truncate_node` 超出上限是直接丢弃旧消息，要改成结构化摘要，并测长对话的事实准确率变化
- **向量库**：接入本地向量库，做长期记忆检索和本地文档 RAG，产出 Recall@k 和 MRR
- **可观测性**：先自建 trace 表记节点耗时和错误，再挂 LangSmith
- **verifier 节点**：拿草稿回答和原始工具结果交叉核对（明确不做 supervisor 多智能体）
- **Agent 最大工具调用次数**：模型会在同一轮重复调用同一个工具，需要一个 guard
- **PostgreSQL 等生产级 Checkpoint**
