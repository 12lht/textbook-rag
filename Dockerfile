# 教材助教（Agent Workspace）运行镜像。
#
# 设计取舍：
# - **不用 slim 之外的花样**：解析链（pymupdf / python-pptx / python-docx）
#   都有 manylinux wheel，不需要编译工具链，省下几百 MB。
# - **先拷 requirements 再拷代码**：改代码不会让依赖层失效，重建只需几秒。
# - **非 root 运行**：容器里跑 root 一旦有目录穿越类问题，影响面完全不同。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据目录得由运行用户可写。这些路径在运行时应当被卷覆盖（见 docs/DEPLOY.md），
# 这里先建出来是为了「不挂卷也能起来看看到底缺什么」。
RUN useradd --create-home --uid 10001 agent \
    && mkdir -p /app/data /app/out \
    && chown -R agent:agent /app/data /app/out
USER agent

EXPOSE 8000

# 只查 /api/health：它会走完整个应用栈（不碰模型、不碰数据库），
# 比 TCP 端口探测更能说明「这个进程是好的」。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)"

# --------------------------------------------------------------------------
# --workers 1 是硬约束，不能改成 4 或 "auto"
# --------------------------------------------------------------------------
#
# 改之前先看这三条，每一条单独都足以让多进程出错：
#
# 1. **写状态的地方不是进程安全的**：会话元数据是 `data/` 下的 JSON 文件，
#    每次写是全量重写；限流是进程内 `dict`。两个 worker 会互相覆盖，
#    表现是「会话列表偶尔少一个」「每分钟额度实际是 2 倍」。
# 2. **配额在 SQLite**：单写者本来能扛，但配合上面那条的重复计数就没意义了。
# 3. **MCP 客户端是常驻 asyncio 任务 + stdio 子进程**：每个 worker 各起一份，
#    工具会话不共享，退出时的清理也会互相踩。
#
# 要横向扩，方向是「把 JSON 元数据换成 Postgres + 限流移到 Redis +
# MCP 换成独立 HTTP 服务」，那是另一次改造，不是加个参数。
#
# --proxy-headers / --forwarded-allow-ips：挂在反向代理后面时，
# 让 uvicorn 采信 X-Forwarded-Proto（决定 cookie 是否加 Secure）和
# X-Forwarded-For（访客按 IP 限流的依据）。**只在「除了反代没人能直连」
# 的网络里用 `*`**；如果能绕过反代直连 8000，客户端就能自填 XFF 伪造来源。
CMD ["uvicorn", "api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--no-server-header"]
