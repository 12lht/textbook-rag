# 部署

这份文档只讲**怎么把它跑在公网上**，以及每一步为什么这么做。
「为什么这么设计」在 `ARCHITECTURE.md`，接口在 `API.md`。

---

## 0. 三种跑法，选一种

| 形态 | 适合 | 代价 |
|---|---|---|
| 本机 `uvicorn` | 自己用、改代码 | 只有 `127.0.0.1` 可达 |
| Docker + 反代 | **给同学用（推荐）** | 要一台服务器、一个域名 |
| 不做容器、直接 systemd | 已有 Python 3.11 环境 | 依赖靠手工维护 |

**不建议打包成桌面 exe**：MCP 是 stdio 子进程，解析链要 pymupdf/pptx/docx
（300MB+），PyInstaller 打出来体积会爆；而它本来就要联网调模型，套壳没有收益。
想要「像个应用」，用域名 + PWA 就够了。

---

## 1. 环境变量

`.env` 放在项目根目录（已被 `.gitignore` 排除）。**只有前两行是必需的。**

| 变量 | 必需 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 是 | 主模型。`MODEL_PROVIDER=glm` 时改用 `GLM_API_KEY` |
| `TAVILY_API_KEY` | 是 | 联网搜索工具。没配则搜索工具不注册、按钮置灰 |
| `AMAP_API_KEY` | 否 | 天气 + 逆地理编码。没配则天气工具回「服务未配置」 |
| `MODEL_PROVIDER` | 否 | `deepseek`（默认）/ `glm`。主模型与标题模型共用这一个开关 |
| `ASSET_SECRET` | 否 | 教材插图签名密钥。不配则首次启动生成 `data/asset_secret.key` |
| `GUEST_SECRET` | 否 | 访客 cookie 签名密钥。同上，生成 `data/guest_secret.key` |
| `EMBEDDING_API_KEY` | 否 | 检索向量的 key。不配则退回纯 BM25（检索仍可用） |
| `RERANK_ENABLED` | 否 | 设 `0` 关掉 rerank（省一次外部调用） |
| `LOG_LEVEL` | 否 | 默认 `INFO`，排障时设 `DEBUG` |

> **两个 `*_SECRET` 要么都配、要么都不配（让它落盘）。**
> 配了就必须一直用同一个值——换掉之后，旧的图片签名和所有访客 cookie 立刻失效。
> 多机部署时必须显式配成同一个值，否则每台机器签出来的不一样。

---

## 2. Docker 起服务

```bash
# 1. 数据目录要先存在，并把属主交给容器里的运行用户（uid 10001）
mkdir -p data out backups
sudo chown -R 10001:10001 data out

# 2. 配好 .env（见上一节）

# 3. 起
docker compose up -d --build

# 4. 看
curl --noproxy '*' http://127.0.0.1:8000/api/health     # {"status":"ok"}
docker compose logs -f app
```

`compose` 把端口绑在 `127.0.0.1:8000`，**外面进不来**——这是故意的，
所有公网流量必须经过反向代理（下一节）。直接把 8000 暴露到公网等于裸 HTTP，
令牌明文过网。

---

## 3. 反向代理 + HTTPS

三件事必须在反代这一层做，应用里做不了：

| 配置 | 值 | 不这么做的后果 |
|---|---|---|
| `client_max_body_size` | `252m` | 传教材时反代先返回 413，应用层那道上限根本收不到 |
| 响应缓冲 | **关** | SSE 被攒成一大块再发，界面「转半天然后一次全出来」 |
| 读超时 | `600s` 以上 | 长回答被反代掐断，前端表现为「回答到一半没了」 |
| `X-Forwarded-Proto` | 透传 | 应用以为自己在 http，访客 cookie 不加 `Secure` |
| `X-Forwarded-For` | 透传 | 访客按 IP 限流全部落成 `127.0.0.1`，**一个人就能把所有人挡在门外** |

### Nginx

```nginx
server {
    listen 443 ssl http2;
    server_name your.domain.com;

    ssl_certificate     /etc/letsencrypt/live/your.domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/your.domain.com/privkey.pem;

    client_max_body_size 252m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # SSE：不缓冲、不压缩、不放短超时
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }
}

server {
    listen 80;
    server_name your.domain.com;
    return 301 https://$host$request_uri;
}
```

### Caddy（自动证书，更省事）

```caddyfile
your.domain.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1          # 等价于关闭缓冲，SSE 必须
    }
    request_body {
        max_size 252MB
    }
}
```

> Caddy 默认会自动透传 `X-Forwarded-*` 和正确处理长连接，不需要额外写超时。

---

## 4. 硬约束：`--workers 1`

`Dockerfile` 里已经写死。**不要改成 4 或 auto**，三条理由任一条单独都够：

| 冲突点 | 多进程后的表现 |
|---|---|
| 会话元数据是 `data/*.json`，每次全量重写 | 两个会话偶尔「消失」或标题串号 |
| 限流是进程内 `dict` | 每分钟限额实际变成 `N × 20` |
| MCP 是常驻 asyncio 任务 + stdio 子进程 | 每个进程各起一份，退出清理互相踩 |

要横向扩，方向是「元数据进 Postgres + 限流进 Redis + MCP 拆成独立 HTTP 服务」，
那是另一次改造，不是加个启动参数。

**单 worker 够不够用**：够。瓶颈是模型 API 的延迟（秒级），不是 CPU；
一个 worker 的 asyncio 能同时挂几十条 SSE 连接。真正的天花板是模型配额和磁盘。

---

## 5. 备份与恢复

```bash
# 全量（含插图与上传原件）
python scripts/backup.py --keep 7

# 精简（只备状态，约 25MB；assets/uploads 需要时能从 textbook/ 重新解析）
python scripts/backup.py --slim --keep 7
```

**为什么要用脚本而不是 `cp -r data`**：正在写的 SQLite 直接拷文件可能拷到半页，
而你要到恢复那天才发现。脚本走 SQLite 的在线备份接口，拿到一致快照。

`backups/` 建议同步到另一台机器或对象存储——**备份和原数据放同一块盘，
等于没有备份**。

恢复步骤（**先停服务**）见 `scripts/backup.py` 顶部注释。

建议加一条 crontab：

```cron
30 4 * * * cd /srv/agent && /usr/bin/python3 scripts/backup.py --keep 7 >> logs/backup.log 2>&1
```

---

## 6. 上线检查清单

按顺序过一遍，前四条是硬门槛。

| # | 检查 | 怎么验 |
|---|---|---|
| 1 | 未登录访客之间互相看不见 | 两个浏览器各开一次，A 建会话，B 的列表里没有 |
| 2 | HTTPS 生效 | 地址栏是锁，`http://` 自动跳 `https://` |
| 3 | 反代能收 252MB | 传一本 >100MB 的 PDF，进度条走完不报 413 |
| 4 | 备份能跑通并能解开 | `python scripts/backup.py`，解包后 `PRAGMA integrity_check` 全是 ok |
| 5 | 日志有内容 | `docker compose logs` 能看到「启动完成」和请求异常 |
| 6 | 注册要邀请码 | 清空浏览器的 localStorage 后注册，提示填邀请码 |
| 7 | 长回答不被掐断 | 让它写一段 2000 字，从头到尾连续出字，不中途停 |

**第 1 条最容易漏**：本机测不出来（只有一个浏览器），一定要开两个浏览器
（或一个普通窗口 + 一个无痕窗口）验。

---

## 7. 故障对照表

| 现象 | 最可能的原因 |
|---|---|
| 每次刷新都变成新访客、会话「丢了」 | 访客 cookie 没落地。`GUEST_SECRET` 是否多机不一致；反代是否吞了 `Set-Cookie` |
| 界面能开，一聊天就 401 | 令牌过期。前端会清本地令牌，重新登录即可 |
| 图片全部裂开（403） | `ASSET_SECRET` / `asset_secret.key` 变了或丢了 |
| 传书 413，但文件不到 250MB | 反代 `client_max_body_size` 没配 |
| 回答「转很久然后一次全出来」 | 反代缓冲没关（`proxy_buffering off`） |
| 回答到一半断了 | 反代读超时太短 |
| 所有人都被限流 / 额度飞快用完 | `X-Forwarded-For` 没透传，全站共用一个 IP 计数 |
| 接口 404/405，但 `/docs` 里明明有 | 进程跑在旧代码上，重启容器 |
| 会话列表里出现「别人」的会话 | 反代或 CDN 缓存了 JSON 响应（不该有） |
| 重启后书籍一直「解析中」 | 解析任务随进程中断。启动时会自动标成 failed，重传即可 |
