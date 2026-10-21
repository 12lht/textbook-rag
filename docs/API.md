# 接口

全部端点定义在 `api.py`，起服务后可打开 `/docs` 看实时 schema。

## 端点清单

除了 `/api/health`、`/`、`/library`、`/api/location` 和 `/api/auth/*` 之外，其余端点都会按 `Authorization` 头里的令牌决定「看得见哪些会话」。`/api/invites`、`/api/documents`、`/api/categories` 更进一步：它们**要求登录**，访客拿不到（`/api/assets/{id}` 是例外，它靠 URL 里的签名鉴权，因为 `<img>` 带不上 `Authorization` 头）。

## 访客身份（无令牌时）

访客不再共用一个账号，而是**按浏览器**发一个签名 cookie：

| 项 | 值 |
|---|---|
| cookie 名 | `aw_guest` |
| 格式 | `g-<16 hex>.<24 hex 签名>`，签名覆盖 id，改了 id 就验不过 |
| 属性 | `HttpOnly`、`SameSite=Lax`、`Path=/`、30 天；HTTPS 下自动加 `Secure` |
| 什么时候下发 | 任何一个普通 JSON 响应（首页 HTML 也发）。**SSE 那条路发不了**，但它之前必然经过一次普通请求 |
| 伪造 / 过期怎么办 | 不报错，**发一个新的**——访客不该看到错误页 |

访客的会话、配额、清理都以这个 id 为界：彼此看不见，7 天后由启动时的清理删掉，
而且除了每人每天 200 次之外，**全体访客共享一个每日 500 次的池**（清 cookie
换新身份也绕不过）。会话列表里的 `user_id` 以 `g-` 开头即访客，真实账号是 `u-`。

`/api/auth/me` 返回的 `guest` 字段是前端区分「要不要显示登录入口」的唯一依据。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/auth/register` | 注册（成功即自动登录，返回用户 + 令牌） |
| `POST` | `/api/auth/login` | 登录，返回用户 + 令牌 |
| `POST` | `/api/auth/logout` | 撤销当前令牌（**不要求令牌仍有效**，过期时也能登出） |
| `GET` | `/api/auth/me` | 当前身份（游客返回 `guest: true`，不是错误；带 `is_owner`） |
| `GET` | `/api/auth/registration-policy` | 注册要不要邀请码（不需要登录） |
| `POST` | `/api/invites` | 发一个邀请码（只有站点主人能做，其余 403） |
| `GET` | `/api/invites` | 已发出的邀请码（同上，只有主人能看） |
| `DELETE` | `/api/invites/{code}` | 删掉一个邀请码（**删掉即作废**，已用的也能删） |
| `GET` | `/api/memories` | AI 记住的关于你的东西 |
| `PATCH` | `/api/memories/{memory_id}` | 改一条记忆的措辞 |
| `DELETE` | `/api/memories/{memory_id}` | 忘掉一条记忆 |
| `GET` | `/api/conversations` | 获取会话列表 |
| `POST` | `/api/conversations` | 创建新会话 |
| `DELETE` | `/api/conversations/{thread_id}` | 删除会话 |
| `PATCH` | `/api/conversations/{thread_id}` | 重命名会话 |
| `GET` | `/api/conversations/{thread_id}/messages` | 获取历史消息 |
| `POST` | `/api/conversations/{thread_id}/rewind` | 回退到某条用户消息之前 |
| `POST` | `/api/chat/stream` | SSE 流式聊天 |
| `POST` | `/api/location` | 把客户端位置解析成可读城市名 |
| `POST` | `/api/documents` | 上传一本教材（立刻返回 `pending`，解析在后台跑） |
| `GET` | `/api/documents` | 教材列表（含 `status` / 页数 / 段数 / 图表数 / `category_id`） |
| `PATCH` | `/api/documents/{doc_id}` | 改归属：`{"category_id": "c-xxxx"}`，传 `null` 挪回未分类 |
| `DELETE` | `/api/documents/{doc_id}` | 删除教材（正文、图片、向量一起清） |
| `GET` | `/api/documents/{doc_id}/markdown` | 解析出的 Markdown；`?render=1` 把图引用换成带签名的 URL |
| `GET` | `/api/categories` | 分类列表，每个带 `doc_count` |
| `POST` | `/api/categories` | 新建分类（重名 `409`，名字不合法 `400`） |
| `PATCH` | `/api/categories/{category_id}` | 改名（不存在 `404`，撞名 `409`） |
| `DELETE` | `/api/categories/{category_id}` | 删分类，**类下的书退回未分类**，不跟着删 |
| `GET` | `/api/assets/{asset_id}` | 教材插图（签名鉴权，带 `?u=&t=`） |

### 页面

| 路径 | 内容 |
| --- | --- |
| `/` | 对话主界面 |
| `/library` | 知识库管理页（分类 / 入库 / 归类 / 删除 / 看解析） |

### 分类与归属

- 一本书**最多属于一个分类**（`documents.category_id`，可空 = 未分类）。刻意不做多对多：多对多会带来「删分类时书还有别的家吗」这类判据，界面上还要处理「这本书在两个分类里都出现」。
- **删分类不删书**。分类只是一行标签，书是几十 MB 解析出来的资产；响应里带 `documents: "unfiled"` 就是为了让前端能明确告诉用户这一点。
- 归属写在书身上（`PATCH /api/documents/{id}`），不开 `POST /api/categories/{id}/documents`：一本书同时只在一个分类里，入口只有一个才不会互相打架。
- 越权一律 `404`（书不是你的、分类不是你的都算「不存在」）。区分 `403` 等于告诉对方「这个 id 是真的」。

### 上传的体积上限（`POST /api/documents`）

两层闸，命中任意一层都是 **`413`**，`detail` 里给上限：

| 闸 | 时机 | 依据 |
|---|---|---|
| 请求体（`Content-Length`） | 读到第一个字节之前 | 超过 252MB（文件上限 250MB + multipart 开销）直接返回 |
| 边读边数 | 读取过程中 | 没有 `Content-Length`（chunked）时的兜底，累计超限立刻停 |

为什么不能只靠 `documents.MAX_FILE_BYTES`：那个检查发生在文件**全部读进内存之后**，
一个 3GB 的请求会先把进程内存吃光才被告知「文件太大」。
反向代理那层也要配 `client_max_body_size 252m`，否则请求到不了应用（见 `DEPLOY.md`）。


### 会被限流的端点

下表这几个不只是「可能失败」，而是**会主动挡住你**。被挡时返回 `429`，响应头带 `Retry-After`（秒）。

| 端点 | 限制 | 按什么算 |
| --- | --- | --- |
| `/api/auth/login`、`/api/auth/register` | 10 次 / 分钟 | 来源 IP（优先取 `X-Forwarded-For` 第一段） |
| `/api/chat/stream` | 20 次 / 分钟 | user_id；**访客额外按来源 IP 再算一次** |
| `/api/chat/stream` | 200 次 / 天 | 每个身份一份（访客按浏览器算） |
| `/api/chat/stream` | 500 次 / 天 | **全体访客共享一个池**（`pool-guest`） |

访客那两行是配套的：他的身份按 cookie 分配，**清 cookie 就是新身份**，
只按人算的话每分钟和每天的上限都能靠清 cookie 绕过。按 IP 限流和全局池
分别堵住「换 cookie」和「换 IP 但总量有限」两种情况。池满时的文案会引导注册。

`429` 有两种含义，看文案就能分开：**「太快了」**是限流（等几十秒），**「今天用完了」**是配额（等到明天）。前者带 `Retry-After`，后者也带，但等了也没用。

`/api/documents` 的 400 里也可能出现「最多存 20 本书」——那是数量上限，归到 400 而不是 429：它跟「文件太大」是同一类问题，换成明天也不会变。

注册 / 登录请求：

```json
{
  "account": "taoge@example.com",
  "password": "至少 8 位",
  "invite": "AW-XXXX-XXXX"
}
```

`account` 一个字段接住用户名 / 手机号 / 邮箱。`invite` **只有注册时才有意义**，而且只有「站里已经有人」时才必填——第一个账号不用填。前端可以先问 `GET /api/auth/registration-policy`（返回 `{"needs_invite": true|false}`）来决定要不要显示这一栏，不要自己猜。

成功返回：

```json
{
  "user": { "user_id": "u-xxxxxxxxxx", "account": "taoge@example.com", "created_at": "..." },
  "is_owner": true,
  "token": "……",
  "expires_at": "..."
}
```

注册重名或格式不合规 → 400；登录失败 → 401。**「账号不存在」和「密码错误」返回同一句话**，不给攻击者做「这个账号注册了没有」的探测。

重命名请求示例：

```json
{
  "title": "天气与出行的讨论"
}
```

标题会折叠连续空白并截断到 30 字（同一规则也用于自动起标题）。手动改过名字的会话会被标记下来，后续消息不会再触发自动起标题把它覆盖掉；标题为空返回 400，会话不存在返回 404。

自动起标题的**节奏**：第 1 轮先起一次，之后每 10 轮重起一次，到第 50 轮为止——触发点是第 **1 / 11 / 21 / 31 / 41** 轮，一个会话最多调 5 次标题模型，之后标题就固定下来。

规则收在 `service.should_generate_title(turn_number)` **一处**，它是个纯函数（不读状态、不碰模型），所以探针可以直接逐轮测边界，不用真聊 50 轮。

> 这里原来是个**上限**（`AUTO_TITLE_TURNS = 10`），含义却是「前 10 轮**每轮**都重起一次」——名字看着像「前 10 轮起标题」，实际是一个会话调 10 次模型。改成间隔之后同规模会话只调 5 次。

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

### 流式事件

流里每个事件都是 `data: {"type": ..., ...}` 一行。

| `type` | 字段 | 含义 |
| --- | --- | --- |
| `conversation` | `thread_id` | 新建了会话，前端记下 id |
| `title` | `title` | 自动生成的标题（只在第 1/11/21/31/41 轮出现） |
| `reasoning_delta` | `content` | 主模型**思维链**的一个分片 |
| `assistant_delta` | `content` | 回答正文的一个分片 |
| `tool_call` | `tool`、`calls` | 模型要调工具，`calls` 里是名字和参数 |
| `tool_result` | `tool`、`status` | 工具返回，`status` 为 `success` / `error`，前端渲染成「工具完成 / 工具失败」 |
| `error` | `message` | 出错，前端应把它当回答显示并标红 |
| `done` | — | 本轮结束 |

思维链和正文都在它**之前**（一轮里模型先想再答），前端把思维链装进助手气泡上那个「模型思考」面板里的折叠块。

两条通道分工不同，别混着用：`assistant_delta` / `reasoning_delta` 来自 `messages` 通道（逐字、实时），`tool_call` / `tool_result` 来自 `updates` 通道（节点跑完才有、参数完整）。工具调用之所以只从 `updates` 播一次，是因为两条通道的去重键不相等（`index` 是整数、`id` 是字符串），一起播会显示两遍。

`messages` 通道捕获的是**图里所有被调用的模型**，不只是主模型——`truncate` 节点每轮都跑，它在需要裁剪时会调一次模型生成摘要，那次的 token 同样会从这条通道冒出来。所以**必须按 `metadata["langgraph_node"] == "model"` 过滤**，否则用户会在「思考过程」里看到一段与本次提问无关的摘要草稿，正文那条更糟（摘要会被当成回答，出现在真正的答案前面）。判据写成「明确是别的节点才跳过」而不是「必须是 `model` 才播」：前者在 `langgraph_node` 缺失时只是漏过滤，后者会让回答静默变空。

**`status` 走两条路，别只修一条。** 实时渲染用流事件里的 `status`；刷新页面后走的是 `/api/conversations/{id}/messages`，那里的 `status` 由 `agent_core.get_history` 塞进 `operations`。**两条路都要带**，否则「刷新一下，失败就变成成功了」。

⚠️ `status` 只在工具**抛异常**时才是 `error`。本项目的工具按约定**不抛异常**——出错时返回一句可读的话（`web_extract` 打不开域名会返回「无法读取网页：…」，`save_memory` 写不进去会返回「暂时写不进去」）。所以「工具失败」标红实际捕获的是**崩溃级故障**（参数校验不过、代码 bug），**不是**「工具没办成这件事」。要区分后者只能靠模型读返回文本，或者给工具加一套结构化的错误返回——后者会改动 `tools.py` 的全部工具，还没做。

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

