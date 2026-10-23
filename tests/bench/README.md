# bench：系统核心指标基准

测的是**整个系统**（service 层起，含标题模型、记忆注入、MCP），不是 Agent 本身
——绕过 service 测出来的 TTFT 好看但失真。要看 Agent 本身的质量指标
（工具选择合规率等）去 `eval/`，那里是另一套口径。

## 怎么跑

在仓库根目录执行（`bench.py` 自己按 `__file__` 找仓库根，不依赖当前目录）：

```bash
# 冒烟：3 个场景 × repeat 2 ≈ 6 次模型请求，烧少量配额
python tests/bench/bench.py --suite smoke

# 全量：6 场景（含 5 轮连续对话、教材检索、关思考对照）
python tests/bench/bench.py --suite full --repeat 3

# 只跑指定场景
python tests/bench/bench.py --scenarios chat-5turn,rag-textbook --repeat 2
```

| 标记 | 值 |
|---|---|
| 调模型 | **是**（smoke ≈ 6 次，full ≈ 15+ 次） |
| 碰生产库 | 是（临时会话跑完即删；教材归属 `BENCH_USER_ID`，默认 `u-671d242584`） |

产出：`runs/<时间戳>.json`（全量数据）+ `runs/<时间戳>.md`（报告），同时打印到 stdout。

## 指标定义

三层：**采集**（一次提问产出的原始数）→ **派生**（比率）→ **聚合**（P50/P95/冷暖分档）。

### 一、Token 消耗（采集层，来自 provider 返回的 usage，不是估算）

| 指标 | 定义 | 为什么 |
|---|---|---|
| `input_tokens` | 该轮所有模型调用的 input 之和 | 成本主项；多轮工具调用必须累加，只取最后一次会低估 |
| `output_tokens` | 输出 token 之和 | 含思维链 |
| `reasoning_tokens` | 其中思维链占多少 | 计费但用户不可见，是「深度思考」的隐性账单 |
| `model_calls` | 该轮模型被调了几次 | 一次提问 ≠ 一次调用；工具每返一轮就要再问一遍 |

### 二、缓存命中（派生层）

| 指标 | 定义 | 口径说明 |
|---|---|---|
| `cache_read_tokens` | 命中 provider 前缀缓存的 input | DeepSeek 返回 `input_token_details.cache_read`，实测可用 |
| `cache_hit_rate` | cache_read / input | **分母是全部 input**，所以首次必然低——前缀刚写入还没得命中 |
| `cache_miss_tokens` | input − cache_read | 按全价计费的部分 |

**冷/暖分档**（`cold_warm`）：每场景第 1 次记 cold、其余记 warm。注意 cold 的
绝对值受**历史运行**影响——provider 的前缀缓存 TTL 有几个小时，同一个进程或
几分钟前跑过的场景，第 1 次也能吃到命中（实测 full 套件里 chat-5turn cold 命中
63%）。所以 cold/warm 的差值只在「同一次运行内」可比；要看真正的零缓存冷启动，
换一个前缀（比如改系统提示）再跑。

### 三、时延（消费端打点，毫秒）

| 指标 | 定义 | 为什么 |
|---|---|---|
| `ttft_ms` | 发出请求 → 第一个**可见**字（assistant_delta） | 用户感知的「多快开始出字」 |
| `ttft_any_ms` | → 第一个任意 delta（含思维链） | 开思考时可见字会更晚，两个一起看才能分清「在想」还是「卡了」 |
| `total_ms` | → done 事件 | 端到端 |
| `ms_per_output_token` | (total − ttft) / output | 吐字速度，把「答案长」从「答得慢」里剥离出来 |
| `tool_ms` | tool_call → tool_result 时间差累加 | 工具税；并行工具各自计时 |
| `title_ms` | title 事件出现的时刻 | 第 1 轮才起标题，它串行在 Agent 之前，会抬高 TTFT |
| P50 / P95 | 轮次百分位 | 时延是长尾分布，用户投诉的是 P95 |

### 四、成本当量（派生层，无量纲）

```
cost_units = cache_read×1 + cache_miss×4 + output×16
```

比例取自 DeepSeek 官方定价（命中 0.5 / 未命中 2 / 输出 8 元每百万），只保留比例
不写死货币——单价会变，权重关系稳定。换 provider 改 `metrics.COST_WEIGHTS` 三个数。
要换算人民币：拿当时的单价归一化后乘回去。

### 五、健康检查（不是指标，是红线）

每个场景可声明 `expect_tools`：这些工具该被调到却没调到，报告「问题」一节标红。
轮次报错（超时 / error 事件）同理。**指标好看但功能错了没有意义**，所以健康检查
和指标在同一份报告里。

## 场景集（scenarios.jsonl）

| id | 瞄着的指标面 | 说明 |
|---|---|---|
| `chat-1turn` | 基线 | 无工具无历史，最便宜的一轮长什么样 |
| `chat-5turn` | 缓存爬升 | 同会话连问 5 轮，看第 2 轮起命中率与 input 增速 |
| `tool-time` | 最小工具税 | 一个零配额工具，量 schema 固定开销 |
| `tool-weather` | 外部工具税 | 高德往返的真实时延 |
| `rag-textbook` | 检索税 | 检索结果塞进上下文的 token 与时延 |
| `no-reasoning` | 思考税 | 同题关深度思考，对照 output 与 TTFT |

新增场景：往 `scenarios.jsonl` 加一行（字段见文件头注释），再登记进 `bench.py`
的 `SUITES`。

## 文件

| 文件 | 职责 |
|---|---|
| `metrics.py` | 口径纯函数：usage 汇总、比率、百分位、冷暖切分。有自检（零成本），改动先跑 |
| `runner.py` | 执行器：走 `service.chat_events`，消费端打点，从 checkpoint 读 usage |
| `bench.py` | CLI：套件选择、预热计时、聚合、出 JSON + Markdown |
| `scenarios.jsonl` | 场景定义 |
| `runs/` | 输出（已 gitignore） |

## 已知口径边界（读数时要知道的）

1. **标题模型的 token 不在统计内**——它是独立调用、不上 checkpoint。其**时间**
   含在 TTFT 里（第 1 轮场景能看到）。影响：token 成本略有低估，量级 ≈ 每会话一次 400 token 以内。
2. **tool_ms 包含事件转发的几毫秒**——打点在消费端。量级远小于工具本身，不做修正。
3. **usage 从 checkpoint 读**——消息被摘要压缩删除时，`_turn_messages` 从末尾回扫到
   本轮 HumanMessage 为止，不受影响；但压缩动作本身的耗时会计入该轮 total。
4. **冷启动（MCP 握手 + 工具加载）单独计时**，不摊进场景——它是一次性成本，
   摊进任何场景都会让那个场景的读数随启动顺序波动。
