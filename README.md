# Agent Workspace

一个基于 LangGraph、DeepSeek、Tavily 和高德地图 API 的 Agent 学习项目。

当前项目同时提供：

- FastAPI + 网页前端
- CLI 调试入口
- 账号注册 / 登录，会话按用户隔离（游客也能用，只是没有长期记忆）
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
users.py                   账号与登录：账号表、密码哈希、登录令牌（纯应用层，不依赖 Agent）
conversations.py           会话元数据：id、标题、重命名标记、会话归属（纯应用层，不依赖 Agent）
memories.py                跨会话长期记忆：一条事实一行存进 SQLite，去重、清洗、上限（纯应用层，不依赖 Agent）
service.py                 应用层：把上面几层拼成用例（身份收敛/会话用例/回退/对话流/位置收敛/注入记忆后端）
api.py                     FastAPI 后端
cli.py                     CLI 调试入口
cassette.py                评测用的工具录制回放
eval/cases.jsonl           评测金标集（32 个用例）
eval/run.py                评测 runner
eval/baselines/            历史基线 JSON
eval/cassettes/            录制下来的工具返回值
frontend/index.html        网页前端
data/                      SQLite（checkpoint / 长期记忆 / 账号）和会话元数据 JSON
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

### 什么工具不能放进 MCP

先说准确，否则容易得出「协议做不到」的错误结论。

**MCP 协议是留了逐次调用的旁路通道的**：`CallToolRequest.params._meta`，在 SDK 里就是

```python
await session.call_tool(name, arguments, meta={...})
```

**挡住它的是框架层，不是协议。** 本项目用的 `langchain-mcp-adapters`：

- 它的 `execute_tool` 调 `session.call_tool` 时**不传 `meta`**；
- interceptor 能覆盖的字段只有 `name` / `args` / `headers`——**不含 `meta`**。

所以想在 MCP 里传「本次请求的用户位置」，得绕开适配器自己写一层 MCP 工具包装（schema、结果转换、错误处理全得自理）。

**但即使通道打通，也不该这么做。** 因为那个工具仍然要模型**先决定去调它**，才能拿到一个本来可以直接被告知的事实——多一次模型往返、多一个失败点、多一次计费调用。位置属于编排层的输入，不是需要去探测的外部世界。

判据就一句话：

> **返回值只由参数决定的工具放 MCP；需要「本次请求」上下文的工具留在进程内。**

这是有意识地拿「架构一致性」换了「数据正确性」：`get_user_location` 留在 `agent_core.py`，用 `runtime: ToolRuntime[Context]` 读调用方传进来的位置。它原先在 MCP server 里，靠高德 **IP 定位**（`/v3/ip`）去猜用户在哪，有两个各自独立的问题：

1. 它定位的是**请求来源 IP**。服务一旦多用户部署，所有用户都会被猜成服务器所在地，模型会对每个人断言「你当前在北京」——这是在编造用户事实。单机自用「碰巧对」，掩盖了这个缺陷。
2. 即使猜对了，也多花一次模型往返 + 一次计费的高德调用，去问一个本可以被告知的答案。

**反过来看**：如果哪天工具要独立部署、跨语言实现、或者单独授权（MCP 的真正价值），那"留在进程内"才是不行的那个。这条判据是双向的，不是"MCP 不好"。

位置怎么进来这条链路：

```text
浏览器 geolocation（用户授权）        用户手动选城市（拒绝授权时的兜底）
        │ 坐标                        │ 城市名
        └──────────┬───────────────────┘
                   ▼
        service.resolve_client_location   ← 优先级规则只写在这一处
                   │  可读文本，如「广东省广州市」
                   ▼
        agent_core.Context.location
                   │  ToolRuntime 注入（模型看不到这个参数）
                   ▼
  ①  模型调 get_user_location（参数为空）
                   │  工具返回「用户授权提供的当前位置：广东省广州市」
                   ▼
  ②  模型调 get_weather_for_location(city="广东省广州市", mode=...)
                   │  注意 city 仍然是模型自己填的，位置只是帮它省掉了「问用户」
                   │  （实测模型连调了两次：mode=current 和 mode=forecast）
                   ▼
  ③  工具返回天气，模型组织成回答
```

**第 ① 步的参数是空的**：`get_user_location` 在模型看到的名片上是 0 个参数，`runtime` 由框架在调用那一刻注入。但第 ② 步天气工具的 `city` 仍然是**模型自己填**的——位置只是省掉了它去问用户，不是省掉了它填参数。这一条不能改：如果把 `city` 也换成框架注入的当前位置，这个工具就只会回答「这里的天气」，问「明天适合去广州出差吗」就答不了了。展开见 `learning-notes.md` §21。

两个高德 API 的坑：

- **高德 `location` 参数是「经度,纬度」**，和浏览器 `{latitude, longitude}` 顺序**正好相反**。传反了不报错，只会安静地定位到地球另一边。
- **直辖市的 `city` 字段是空列表 `[]` 而不是空字符串**（北京 / 上海 / 天津 / 重庆），广州 / 成都这类才给 `'广州市'`。取字段前必须判类型，否则会拼出「北京市[]」。

客户端位置**不写进 checkpoint**：它是每次请求的输入，不是会话状态。用户换个地方，下一轮就该变。

坐标解析走 `geocode.py`，坐标取两位小数（约 1 公里）做缓存键。解析发生在 `service` 层，用 `asyncio.to_thread` 丢进线程池——它是阻塞的 `urllib` 调用，直接在事件循环里跑会卡住所有并发请求，包括正在流式输出的那条 SSE。

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

CLI **不走登录**：它是本机单人使用，直接用 `service.CLI_IDENTITY`（默认账号 + **开着**记忆）。网页侧的游客用的是同一个默认账号但记忆是关的——关记忆的理由是「几个陌生游客共用一个账号会互看偏好」，本机单人没有这个问题，所以两个场景下同一个 `user_id` 的 `memory_enabled` 不同。这也是为什么这个开关是逐请求的、而不是绑在身份上。

## 账号与登录

**不登录也能用**——没带令牌就是「游客」，仍然能聊天、能建会话。登录之后多两件事：会话按用户隔离，以及开启跨会话的长期记忆。

```text
Authorization: Bearer <token>
        │
        ▼
api.bearer_token          只负责从头上把令牌抠出来
        │
        ▼
service.resolve_identity  ← 「你是谁」的唯一入口（这一层才判有效性）
        │  Identity(user_id, account, memory_enabled)
        ▼
会话用例（按 user_id 过滤）/ 聊天（带上 memory_enabled）
```

### 账号

`account` 一个字段同时接住用户名 / 手机号 / 邮箱（字母数字下划线点 @ + -），不让用户先选「用哪种方式登录」。

- `account` 存**原文**（界面显示用），`account_key` 存归一化后的值（`strip().lower()`，UNIQUE）。用户叫 `TaoGe` 就该显示 `TaoGe`，但注册 `taoge` 必须被挡住。
- `user_id` 是**与账号无关的不透明 id**（`u-xxxxxxxxxx`）。不拿账号当主键——账号以后可能允许修改，而「这个人的历史数据」不该跟着变。
- 判重靠 `INSERT` 撞唯一约束，**不靠「先查再插」**：先查再插中间有一个窗口，两个并发注册会同时通过检查。

### 密码怎么存

用标准库 `hashlib.pbkdf2_hmac`（OpenSSL 实现，纯 C），存成带版本前缀的字符串：

```text
pbkdf2_sha256$600000$<salt_b64>$<hash_b64>
```

- **不存明文、不做可逆加密**。加密意味着有密钥，而密钥就在同一个仓库里，等于没加密。
- **每个用户一个随机 salt**。否则两个人密码相同就是同一个哈希值，一眼能看出来，彩虹表也直接通用。
- **迭代次数写进字符串**。以后调高（机器变快、或嫌慢），老记录仍按它自己记的次数验证通过，不需要强制所有人改密码——校验时**从存储值里读参数**，不用当前的全局常量，这是版本前缀的全部意义。
- 实测一次哈希 113ms（600000 次迭代）。

两个细节：

- **账号不存在时也跑一次哈希**（拿一个 `_DUMMY_HASH` 陪跑）。否则「账号不存在」立刻返回、「密码错」要花 113ms，这个时间差就是一个「该账号已注册」的探测器。实测两条路都是 118~119ms。
- 比较摘要用 `hmac.compare_digest`：普通的 `==` 在第一个不同的字节处提前返回，理论上可以被一个字节一个字节地试探。

### 令牌

登录成功后签发 `secrets.token_urlsafe(32)`（约 256 位随机），**原文只在这一次响应里返回**，库里只存它的 sha256 摘要。

不需要 salt 和迭代：令牌本身就是高熵随机值，猜不出来，也就不怕彩虹表——哈希只是为了「库被人看到」不等于「拿到一批能用的令牌」。

- **过期用绝对时间**（登录时定死 30 天），不做滑动续期。
- **`resolve_token` 是纯读**。想顺手刷新 `last_seen` 的话，每个请求都会多一次写事务，而实测一次提交在这个环境要 ~30ms（成本全在回滚 journal 的建删文件上）。要滑动续期得改成「距上次刷新超过 N 分钟才写」。
- 过期令牌行由 `service.startup()` 在进程启动时顺手清一次（没有定时任务）。

### 令牌不认时报 401，不降级成游客

这两件事必须分开：

| 情况 | 处理 | 为什么 |
| --- | --- | --- |
| 没带 `Authorization` 头 | 游客身份，正常使用 | 未登录是正常状态，不是错误分支 |
| 带了令牌但不认（过期 / 撤销 / 伪造） | **401** | 静默降级成游客会让用户看到**游客名下的会话**，还以为那是自己的 |

前端收到 401 就清掉本地令牌、退回游客态并提示重新登录。`Authorization` 头存在但格式不对（缺 `Bearer` 前缀）同样报 401——客户端明明想带凭证却被当成游客，会让「头部拼错了」表现成「登录态时有时无」，极难排查。

### 越权返回 404，不返回 403

**别人的会话一律报「不存在」。** 403 等于告诉对方「这个 thread_id 是真的，只是不属于你」，可以拿它枚举别人的会话 id；404 什么都不泄露，而且从用户视角看，别人的会话本来就等于不存在。

所有越权判断只走 `service._require_owned` 一个入口，存在性和归属**一起判**——写成「先查存在、再查归属」两段式，中间一有别的分支就很容易只判了前一半。

### 客户端不能再自报 `user_id`

以前 `ChatRequest` 里有个 `user_id` 字段（默认 `"1"`），已经**删掉**。身份的唯一可信来源是服务端签发的令牌，请求体里的 `user_id` 会被直接忽略。同理，聊天的归属检查放在开流**之前**做（`service.begin_chat`）——SSE 一旦开始，响应头就已经发出去了，之后只能往流里塞 error 事件，前端拿到的是 200，没法用状态码统一处理。

### 游客共用默认账号，所以不隔离

`GUEST_USER_ID = "1"`，所有未登录请求都挂在它名下。因此：

- **游客之间不隔离**——他们共用会话列表。这是加登录之前就有的行为（那时所有请求都硬编码 `user_id = "1"`），不是本次疏忽；要隔离只能给游客发匿名令牌。
- **游客的长期记忆是关的**。否则几个陌生人会互相读到对方记下的偏好——那就不只是「看到会话」，而是「看到别人的偏好」。
- 改造之前建的会话在归属文件里没有记录，一律按默认账号处理——它们当时本来就属于 `"1"`，这是把旧事实补上，不是猜。兜底只写在 `conversations._owner_from` 一处。

### 关掉记忆必须**明说**，不能只是「不提」

这是实测踩出来的。工具列表是**进程级冻结**的（建 MCP 会话时算好，一进程一份），所以游客这一侧照样看得见 `save_memory` 的说明书（工具描述就是 docstring）。只把系统提示退回 `SYSTEM_PROMPT` 的那版上线后：

> 问：用一句话说明你是什么。
> 答：我是一个……**能记住你长期偏好**以备后续对话使用的 AI 助手。

模型读的是工具说明书。等用户真说了值得记的事、工具回一句「没开启」，模型才反应过来，可承诺已经出口了。所以逐请求关掉记忆时会拼上 `MEMORY_OFF_GUIDE` 明确告诉模型「这次记不住」，并让它如实告诉用户「需要先登录」。加上之后同一句话的回答变成：

> 我是本次对话中为你提供帮助的 AI 助手……但当前**没有开启长期记忆**，所以这次聊完就忘了。

工具的**描述文本**目前仍然是进程级冻结的（按请求换工具表要改 `bound_model` 的缓存结构，代价大），所以这道防线有两层：提示词劝住模型别调，工具里那道检查兜底——真调了会返回「本次对话没有开启长期记忆」，不会写进库。

## API 接口

除了 `/api/health`、`/`、`/api/location` 和四个 `/api/auth/*` 之外，其余端点都会按 `Authorization` 头里的令牌决定「看得见哪些会话」，无令牌即游客。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/auth/register` | 注册（成功即自动登录，返回用户 + 令牌） |
| `POST` | `/api/auth/login` | 登录，返回用户 + 令牌 |
| `POST` | `/api/auth/logout` | 撤销当前令牌（**不要求令牌仍有效**，过期时也能登出） |
| `GET` | `/api/auth/me` | 当前身份（游客返回 `guest: true`，不是错误） |
| `GET` | `/api/conversations` | 获取会话列表 |
| `POST` | `/api/conversations` | 创建新会话 |
| `DELETE` | `/api/conversations/{thread_id}` | 删除会话 |
| `PATCH` | `/api/conversations/{thread_id}` | 重命名会话 |
| `GET` | `/api/conversations/{thread_id}/messages` | 获取历史消息 |
| `POST` | `/api/conversations/{thread_id}/rewind` | 回退到某条用户消息之前 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |
| `POST` | `/api/location` | 把客户端位置解析成可读城市名 |

注册 / 登录请求：

```json
{
  "account": "taoge@example.com",
  "password": "至少 8 位"
}
```

`account` 一个字段接住用户名 / 手机号 / 邮箱。成功返回：

```json
{
  "user": { "user_id": "u-xxxxxxxxxx", "account": "taoge@example.com", "created_at": "..." },
  "token": "……",
  "expires_at": "..."
}
```

注册重名或格式不合规 → 400；登录失败 → 401。**「账号不存在」和「密码错误」返回同一句话**，不给攻击者做「这个账号注册了没有」的探测。
| `POST` | `/api/conversations` | 创建新会话 |
| `DELETE` | `/api/conversations/{thread_id}` | 删除会话 |
| `PATCH` | `/api/conversations/{thread_id}` | 重命名会话 |
| `GET` | `/api/conversations/{thread_id}/messages` | 获取历史消息 |
| `POST` | `/api/conversations/{thread_id}/rewind` | 回退到某条用户消息之前 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |
| `POST` | `/api/location` | 把客户端位置解析成可读城市名 |

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
  "location": { "latitude": 39.90923, "longitude": 116.397428 }
}
```

`thread_id` 为空时，后端会自动创建新会话。后续请求使用返回的 `thread_id` 即可继续对话。

请求体里**没有 `user_id`**：身份只认 `Authorization` 头里的令牌，客户端自报的 `user_id` 会被忽略（字段已从 `ChatRequest` 中删除）。拿别人的 `thread_id` 来聊会直接 404，且在 SSE 开流之前就返回。

### 位置怎么传

`location` 是可选的，两种来源二选一或都给：

| 字段 | 来源 | 说明 |
| --- | --- | --- |
| `latitude` / `longitude` | 浏览器 geolocation（需用户授权） | 后端用高德逆地理编码解析成城市名 |
| `city` | 用户手动选择 / 输入 | 拒绝授权或浏览器不支持时的兜底 |

**优先级：`city` > 坐标。** 手选是用户明确确认过的，坐标可能来自 VPN 或粗粒度 Wi-Fi 定位，可靠性反而更低。规则只写在 `service.resolve_client_location` 一处，`/api/location` 和聊天链路共用。

不传 `location` 就是「用户没有提供位置」，**后端不会拿 IP 去猜**——详见上面「什么工具不能放进 MCP」。

坐标解析单独开 `/api/location` 端点，是因为聊天走 SSE 流、坐标在那一轮里只会进模型上下文，前端拿不到结果去回显「定位到：北京市」。解析不出来返回 `{"location": null}`，不是错误。

```json
{ "latitude": 39.90923, "longitude": 116.397428 }
```

网页上输入框左侧有个定位按钮：点击请求浏览器授权，被拒绝或失败会弹出手动输入城市，选择记在 `localStorage` 里。只有用户**以前就已授权**时才在页面加载时静默取一次位置，不会一打开就弹权限框。

## 数据持久化

会话数据位于 `data/`：

```text
data/agent_checkpoints.sqlite
data/conversation_summaries.json
data/conversation_renames.json
data/conversation_owners.json
data/users.sqlite3
data/memories.sqlite3
```

SQLite 保存 LangGraph 的消息状态，JSON 保存会话列表中的可读摘要。三个 JSON 各管一件事：`conversation_summaries.json` 是扁平 dict（`thread_id -> 标题`），被 `/api/conversations` 和前端直接消费，所以「重命名标记」和「会话归属」各自单独一个文件，格式一样都是 `{thread_id: 值}`——往里加字段会牵动太多地方。

`users.sqlite3` 存账号和登录令牌（两张表；令牌只存 sha256 摘要）。`memories.sqlite3` 一条事实一行地存跨会话的长期事实（老版本的 `data/memories.json` 会在第一次启动时自动导入，然后改名成 `memories.json.migrated`）。删除这些文件会清空本地会话记录、账号和长期记忆。

`conversation_owners.json` 里**没有记录**的会话按默认账号（游客）处理，不做迁移写入——那是改造前会话的真实归属。这个文件也不需要清理残留：`ids_of_user` 以会话表为准遍历，会话删了记录留着也不会「复活」。

## 长期记忆

分两层：

| 层次 | 范围 | 状态 |
| --- | --- | --- |
| 会话内 | 同一个 `thread_id`，几十轮 | 已做：超出窗口的部分压成摘要（`AgentState.summary`） |
| 跨会话 | 换会话还认识你 | 已做：模型自己决定记不记（`save_memory` / `recall_memory`） |

三层各管一件事，跨层组合只写在 `service.py`：

```text
memories.py            一条事实一行存进 SQLite，去重/清洗/上限（零框架依赖）
        ▲
        │  service 启动时 set_memory_backend() 把读写函数注入给 Agent
        │
agent_core.py          两个进程内工具：模型自己判断该不该记，不碰存储、不 import memories
```

几个刻意的选择：

- **窗口按「轮」裁，不按「条」裁**。一轮 = 一条用户消息及其引发的全部回复。保留最近 20 轮，攒到 30 轮才裁一次（**迟滞是必需的**——一超限就裁会导致每轮都多调一次摘要模型），另有 200 条硬顶防止工具密集的会话撑爆 token。用条数当阈值的问题是「100 条」在纯聊天里是 50 轮、在工具密集的会话里只剩十几轮。
- **记不记由模型判断，不由代码定时抽**。早先是每轮对话结束后偷偷用模型提炼一次：成本不可控（不管有没有值得记的都要付一次调用），而且只能追加——解不了「用户改主意」。改成工具之后，`replaces` 让模型能直接作废旧条目。
- **存储实现靠注入，不靠 import**。`agent_core` 不许 import 业务存储（`conversations.py` 就是这条规矩的产物），所以 `service` 把 `memories` 的读写函数当参数传下去。
- **失败必须静默**。存储出错时工具返回一句「暂时写不进去」而不是抛异常——辅助功能不该有把整轮对话搞崩的权力，和 `generate_title` 拿不到标题就退回用户原话是同一个态度。
- **去重只做机械匹配**（去空白、去标点、转小写），同一条反复记只累加 `hits`。「不吃香菜」和「受不了香菜」算不算一条？**现在不算，代码里也没假装算**——那是上向量库时的事。
- **「读」是按需的，不做自动带入**。模型要用了才调 `recall_memory`，代码不会在请求开始时把记忆塞进系统提示。**同一会话的窗口内不需要重读**：那条事实至少在上下文里有两份（用户自己的原话、模型 `save_memory` 时填的参数），实测连着三轮需要它的对话一次都没重新读。真正需要重读的只有两种情况——超出窗口被裁掉（>20 轮），以及**换了一个新会话**。后一种现在完全依赖「模型想起来去读」，这是已知的缺口（见「待学习」里的自动带入 / top-k 检索）。
- **存储层不需要写锁，初始化需要**。去重和累加都在一个事务里，原子性由 SQLite 保证。但库文件不存在时多个线程同时 `connect` 会互相抢着建文件（报 `attempt to write a readonly database`，很像权限问题其实不是），所以 `_connect` 里对「冷启动」做了双重检查加锁。
- **工具的 docstring 就是提示词**。它会被原样当 description 发给模型，所以改 docstring 等于改提示词——**改完必须跑一次真实对话回归**（`probe-memory-live.py`），不能只看代码。
- **开关分两种粒度，不能混成一个**。存储后端是**进程级**注入的（`set_memory_backend`，进程活多久就多久变）；「这个人能不能用记忆」是**逐请求**的（`Context.memory_enabled`），因为同一个进程里登录用户要能记、共用一个默认账号的游客不能记。混成一个就会出现「为了挡游客，把登录用户的记忆也关了」。
- **逐请求关掉记忆时，提示词必须主动说明**，否则模型会照着工具说明书对用户说「我记住了」。详见上面「账号与登录」最后一节。

## 工具

7 个工具，**分两处**：

| 工具 | 作用 | 跑在哪 |
| --- | --- | --- |
| `get_weather_for_location` | 通过高德查询实时天气或未来预报（`mode=current` / `forecast`） | MCP 子进程 |
| `get_user_location` | 读用户授权提供的当前位置 | **Agent 进程内**（要请求上下文） |
| `get_current_time` | 获取当前时间 | MCP 子进程 |
| `web_search` | 通过 Tavily 搜索网页（`topic=general` / `news`） | MCP 子进程 |
| `web_extract` | 通过 Tavily 提取网页正文 | MCP 子进程 |
| `recall_memory` | 读出这个用户的长期记忆 | **Agent 进程内**（要 `user_id`） |
| `save_memory` | 记下一条长期事实，可用 `replaces` 作废旧条目 | **Agent 进程内**（要 `user_id`） |

前 4 个的实现在 `tools.py`、注册在 `mcp_server.py`，Agent 侧通过 MCP 加载；后 3 个都在 `agent_core.py`。分界依据见上面「什么工具不能放进 MCP」——判据是**返回值只由参数决定就放 MCP，需要本次请求上下文的就留在进程内**。

两个记忆工具**只在注入了存储后端时才注册**（`agent_core.set_memory_backend`，由 `service.py` 调用），系统提示里的记忆说明也只在此时拼上。所以评测环境拿到的工具集与提示词和基线逐字一致。

> 注意这是**进程级**的注册开关，和「这次请求允不允许用记忆」（`Context.memory_enabled`）是两件事。后端注入之后就一直在，游客那次请求里工具**仍然挂在工具表上**，只是提示词改成了明确说明「这次记不住」，调用也会被拒。要按请求换工具表得改 `bound_model` 的缓存结构，代价不划算。

注册顺序刻意与改造前保持一致（有些模型对工具顺序敏感）。合并两处工具后由 `agent_core._TOOL_ORDER` 统一排序——**它是工具顺序的唯一来源**，新增工具要登记进去。不要在 `api.py` 或 `cli.py` 里复制工具实现。

MCP 那 4 个还套了一层 `cassette.recorded` 用于评测录制回放，默认模式 `off` 时行为和没套一样。定位工具不套：它不发外部请求，读了位置就返回。

## 评测

金标集在 `eval/cases.jsonl`，32 个用例分 8 类（天气 / 定位 / 时间 / 搜索 / 网页提取 / 多工具 / 负例 / 工具边界）。每条用例两个字段：`expect_tools` 是必备工具，`allow_tools` 是额外允许的工具（语义见下）。

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

# 指定评测时模拟的客户端位置（默认 北京市；传空字符串表示不提供位置）
C:\Users\13526\miniconda3\envs\agent\python.exe .\eval\run.py --location 广东省广州市
```

`--location` 走的是 `Context` 而**不是工具参数**，所以完全不碰 cassette、也不计入工具输出指纹。给固定值是为了让定位类用例可复现——原来靠服务器 IP 定位，每次跑可能都不一样。

指标口径：**工具选择合规率**（主指标）、严格集合相等率、工具召回率 / 精确率、负例抑制率、**配额违规调用数**、**冗余调用数**、工具调用成功率、端到端通过率、TTFT P50/P95、端到端 P50/P95、总 token 消耗、重复的 tool_call 事件数。token 数从 checkpoint 里的 `usage_metadata` 读，不额外发请求。

### 工具选择怎么判：分三档，不搞集合相等

一开始用的是「实际调用集合 == 期望集合」。跑下来天气类只有 0.5，看着像模型在乱调工具。把 6 次运行的记录逐条摊开才发现，额外调用是**按措辞触发的稳定策略**：问「上海现在的气温」时 6/6 都不调 `get_current_time`，问「明天适合去广州出差吗」时 6/6 都调。相对时间（今天 / 明天 / 未来三天）本来就需要一个锚点，这不是错误。

再实测代价：`get_current_time` 是纯本地函数、零外部配额，而且和天气工具同批并行下发，**模型往返 +0.00 次、token +19~165**。所以它既不错也不贵。

于是改成三档，判据写在 `cases.jsonl` 的 `allow_tools` 里（它是「**额外**允许」，不含必备）：

| 档 | 含义 | 怎么判 |
| --- | --- | --- |
| 必备 `expect_tools` | 该调的 | 没调全 → **违规** |
| 额外允许 `allow_tools` | 调了也合理，且不烧外部配额 | 计入「冗余调用数」，当成本报，不当错误 |
| 其余 | 越界 | → **违规**，其中走高德 / Tavily 的计入「配额违规调用数」 |

判定额外调用是否白送，只看一条：**它烧不烧外部配额**。`get_current_time` 免费所以默认放行；`get_weather_for_location` / `web_search` / `web_extract` 分别走高德和 Tavily，有配额，不能白送。个别用例可以逐条放宽（比如 `edge-01` 查一个不存在的城市，去搜一下解释「阿斯加德」是什么属于合理兜底），但不搞全局开口子。

> **口径的后续变化：**`get_user_location` 原先在收费那一档，改成读客户端位置之后它不再发任何外部请求（坐标解析在 `service` 层做一次、还带缓存），所以已移出 `METERED_TOOLS`，和 `get_current_time` 同类。
>
> 这让 `weather-forecast-02` 那条的定性变了：模型多调一次定位不再烧配额。但它**仍然不在该用例的 `allow_tools` 里**，所以按现在的口径**依然算违规**——性质是「越界」而不是「浪费配额」。要不要放行属于金标集口径的决定，没有擅自改。

改口径后用已有基线重算（零 token，`aggregate` 会自动按新规则补算旧记录）：

| | 严格集合相等 | 合规率 | 配额违规 | 冗余调用 |
| --- | --- | --- | --- | --- |
| `baseline-v1` | 0.8125 | **0.9688** | 1 | 6 |
| `baseline-v2-mcp` | 0.7812 | **0.9688** | 1 | 8 |

两件事值得注意：一是剩下的唯一一条违规是 `weather-forecast-02`（"明天适合去广州出差吗？"，广州已在问题里给出，还去查一次 IP 定位，纯烧高德配额），说明新口径没有放水；二是 v1 和 v2 在新口径下相等，之前那个「改个 MCP 指标反而变差」的假信号消失了。

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

实测效果（11 个外部内容敏感的用例：天气 / 定位 / 网页提取）：

| 运行方式 | 各次总 token | 运行间极差 |
| --- | --- | --- |
| 未冻结（实时调用） | 27,565 / 23,660 / 23,231 | 18.7% |
| 已冻结（两次回放） | 24,406 / 24,478 | 0.3% |

运行间的噪声压掉了约 60 倍。剩下的 0.3% 来自模型自己这一轮多调或少调了工具——那是**要测的信号**，不该被抹掉。基线里的 `tool_output_digest` 和对比表里的「工具输出指纹」小节就是用来把两种原因分开的：调用序列也变了 → 模型行为差异；调用序列一样但内容不同 → 冻结失效，这次对比不干净。

> 这 11 个用例里没有搜索类，因为 `search-01` 单个用例的 token 是其余全部用例之和的 1.5 倍（模型在同一轮里搜了三次），带上它做三轮验证的成本会翻好几倍。搜索类在工具层面已经单独验证过（`web_search` 的录制回放在跨进程下逐字一致）。

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
