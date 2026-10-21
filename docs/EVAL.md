# 评测

评测运行器是 `eval/run.py`，金标集 `eval/cases.jsonl`，历史基线在 `eval/baselines/`。

## 怎么跑

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

