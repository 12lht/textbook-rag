# 基线怎么读

这个目录里躺着**两种完全不同**的东西，文件名看不出来，所以先分清楚：

| 前缀 | 评的是什么 | 谁生成的 | 结构 |
| --- | --- | --- | --- |
| `latest.json`、`baseline-*.json` | 对话与**工具选择**（该不该调工具、调哪个） | `eval/run.py` | `metrics` / `by_category` / `config` |
| `parse-*.json` | 教材**解析质量**（页锚点、脏段率、图注召回） | `eval/parse.py` | `pdf` / `pages` / `page_anchor_cover` |

两种的指标没有可比性，别拿 `parse-*.json` 去和 `latest.json` 对比。

## `parse-*.json` 的后缀是书名，不是版本号

容易误会，说明白：

| 文件 | 指哪本书 |
| --- | --- |
| `parse-v12.json` | Hello-Algo.pdf |
| `parse-agents-v12.json` | Hello-Agents.pdf |
| `parse-mobile-v10.json` | 第 1 章 移动通信概述 |

也就是说这是**三本书各自的历史快照**，不是同一本书的三个版本。每一组里 `-vN` 的 N 才是"第几次跑"，序号有跳号是正常的（中间有些跑废了没留）。

每本书拿**自己那组里序号最大**的当基准。

## 怎么用

```bash
# 对话基线：跑一轮，和指定的旧基线对比
python eval/run.py --tag my-try --compare eval/baselines/latest.json

# 解析基线：每次跑都会在 baselines/ 下留一个新快照
python eval/parse.py --pdf data/uploads/<id>/source.pdf
```

`eval/run.py` 每次跑完会把结果同时写成 `<tag>.json` 和 `latest.json`，
所以 `latest.json` 永远是"最近一次"的别名，别把它当某个固定版本。

## 什么时候该删

历史快照都进了 git，删了也能翻回来。真正要保留的只有：

- `latest.json`（当前对标）
- 每本书 `parse-*` 组里序号最大的那个
- 明确写进 `docs/EVAL.md` 的里程碑基线

其余都是过程产物，攒着只会让"该跟谁比"变难。清理前先看一眼 `docs/EVAL.md` 有没有引用它。
