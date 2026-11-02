# 书院 · Textbook RAG

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.143.0-teal)](https://fastapi.tiangolo.com/)
[![LangGraph](https://img.shields.io/badge/LangGraph-1.2.12-orange)](https://langchain-ai.github.io/langgraph/)

把专业课教材变成一间能随时发问的书房：整本 PDF 上传后逐页解析、切段、抽图；
提问时它翻到具体那一页，带着页码和原图回答你。日常的天气、定位、联网搜索也在同一段对话里。

## 主要功能

**教材问答**

- 上传 PDF / PPTX / DOCX，MinerU 云端解析 + 本地兜底，切段落、抽插图并保留图注
- 段落同时建 BM25 索引与向量索引，检索后融合 + rerank，避免单路召回漏检
- 回答带 `[c1]` 式角标，点开是原文片段与页码；正文里能直接引用教材插图
- 可以「进入某本书」提问，把检索范围锁在一本教材内

**会话内读图**

- 输入框左侧「+」里有上传图片、从剪贴板粘贴两条入口，也支持把图直接拖进来、或 Ctrl+V
- 截一道题、拍一页书发过去就能问，不必先入库、不必等解析
- 图片随会话走：第二轮不再带图，模型仍记得上一轮看过什么
- 图片只在本次会话可见，不进教材库；上传前在前端缩到长边 1024，兼顾清晰度和传输体积

**藏书阁**

- 按分类管理教材，支持批量归类、删除、看原文
- 列表支持按编号 / 名称自然排序（`第2章 < 第10章`）或按上传时间排序
- 解析状态、失败原因、双栏排版的切分告警都直接标在卡片上

**通用 Agent 能力**

- 天气、逆地理编码定位、当前时间、网页搜索与网页正文提取
- 「深度思考」「联网搜索」两个开关，逐条消息可控
- 跨会话长期记忆：记住你的专业、偏好、正在学的课
- 账号注册 / 登录，未登录也可用访客身份（按浏览器隔离，7 天清理）

## 技术栈

| 层级 | 技术 |
| --- | --- |
| Agent 编排 | LangGraph + LangChain + DeepSeek（备用 GLM） |
| 工具协议 | MCP（stdio 子进程）+ 进程内上下文工具 |
| 后端 | FastAPI + Uvicorn，SSE 流式输出 |
| 前端 | 原生 HTML/JS，无构建步骤；marked + KaTeX + DOMPurify |
| 教材解析 | MinerU（云端）+ PyMuPDF / python-pptx / python-docx（本地兜底） |
| 检索 | BM25 + 向量融合 + rerank |
| 持久化 | SQLite（checkpoint、账号、会话、记忆、教材） |
| 部署 | Docker + Docker Compose |

## 项目结构

```text
api.py              FastAPI 后端
service.py          应用层组合
agent_core.py       LangGraph Agent 编排、教材检索工具、长期记忆工具
tools.py            纯函数工具（天气、时间、网页搜索/提取）
mcp_server.py       MCP server
geocode.py          高德逆地理编码
documents/          教材库（包，对外只有 `import documents`）
  __init__.py         门面：只做再导出，不写逻辑
  core.py             上传 / 切分 / 存储 / 查询 / 资源（编排者）
  parse.py            PDF / PPTX / DOCX → 元素序列（不碰数据库、不切 chunk）
  db.py               SQLite 连接、建表与迁移，以及全部存储路径
  text.py             Markdown 正文修复：裸 LaTeX、项目符号、符号字体
  categories.py       分类：建、改名、删、归属
  types.py            解析链路的中间数据形态与异常
mineru.py           MinerU 云端解析客户端
retrieval.py        BM25 + 向量融合检索
figures.py          视觉模型调用（图注、图像描述）
attachments.py      会话内临时附件（图片存储、签名访问、过期清理）
users.py            账号与登录
conversations.py    会话元数据
memories.py         跨会话长期记忆
limits.py           配额与限流
cassette.py         评测录制回放
cli.py              CLI 调试入口
frontend/           网页前端，零构建（改完刷新即生效，不需要打包）
  index.html          对话页（只留结构）
  app.css / app.js    对话页的样式与逻辑
  library.html        藏书阁（只留结构）
  library.css / .js   藏书阁的样式与逻辑
  theme.css           两页共用的「纸与墨」颜色与字体令牌
eval/               金标集、评测脚本、基线
tests/              离线回归与探针
docs/               架构、接口、评测、部署、决策记录
```

## 快速开始

```bash
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env      # 填入 DEEPSEEK / AMAP / TAVILY / SILICON / MINERU 的 Key

# 3. 启动
python -m uvicorn api:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。各变量的作用见 [`.env.example`](.env.example)，
不配某个 Key 时对应的工具会自动关闭、不影响其余功能。

```bash
pytest                          # 回归测试（默认不跑需要外部资源的用例）
pytest -m requires_model        # 加上真实模型调用（会花配额）
python eval/run.py --tag v3     # 金标评测（调用真实模型）
python cli.py                   # 命令行调试
ruff check .                    # 静态检查（pip install -r requirements-dev.txt）
mypy                            # 类型检查（只查 pyproject.toml 里 files 列的模块）
```

## 文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 架构与数据流
- [`docs/API.md`](docs/API.md) — 接口与 SSE 事件
- [`docs/EVAL.md`](docs/EVAL.md) — 评测口径与基线对比
- [`docs/DEPLOY.md`](docs/DEPLOY.md) — Docker 部署与公网配置
- [`docs/FEATURE-PROCESS.md`](docs/FEATURE-PROCESS.md) — 新功能从调研到验收的流程
- [`docs/adr/`](docs/adr/) — 架构决策记录
- [`tests/README.md`](tests/README.md) — 测试脚本索引
