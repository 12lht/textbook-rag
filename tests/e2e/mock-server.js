const http = require('http');
const fs = require('fs');
const path = require('path');
const os = require('os');

const { serveStatic } = require('./_static');

const FRONTEND = path.join(__dirname, '../../frontend/index.html');
const ORIGINAL = path.join(os.tmpdir(), 'index.original.html');
const PORT = 8766;

const SENT = '这是一段用于验证流式输出时滚动跟随行为的文本，它会不断追加到当前消息中，使页面持续变长。';
const CHUNKS = [];
for (let i = 1; i <= 200; i += 1) CHUNKS.push(`第 ${i} 段。${SENT}`);

function handle(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');

  if (url.pathname === '/' || url.pathname === '/library') {
    const file = url.pathname === '/library'
      ? path.join(__dirname, '../../frontend/library.html')
      : FRONTEND;
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(fs.readFileSync(file));
    return;
  }

  // `/original` 是"改造前的旧页面"快照，供几个测试做 A/B 对照。
  // 快照放在系统临时目录里、随时可能被清掉，缺了要让测试自己记录
  // 「没有对照组」，而不是把整个 server 崩掉——一崩，后面所有用例都连带失败。
  if (url.pathname === '/original') {
    let snapshot;
    try {
      snapshot = fs.readFileSync(ORIGINAL);
    } catch {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
      res.end('no /original snapshot');
      return;
    }
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(snapshot);
    return;
  }

  // 静态资源走共享模块，/static/* -> frontend/，与后端挂载一致。
  if (serveStatic(req, res)) return;

  if (url.pathname === '/api/auth/me') {
    // e2e 假身份：聊天页需要游客身份才能点出登录弹窗。
    // 注意 library 页的登录门要求非游客——它有专门的 e2e 场景时再扩展这里（如按 referer 区分）。
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ guest: true }));
    return;
  }

  // 页面启动时会问一次「哪些能力可用」。不给它，控制台会一直挂一条 404，
  // 而这条 404 会淹没真正的问题。
  if (url.pathname === '/api/capabilities') {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ reasoning_toggleable: true, web_search_available: true }));
    return;
  }

  if (url.pathname === '/api/conversations') {
    const count = Number(url.searchParams.get('n') || 3);
    const list = [];
    for (let i = 1; i <= count; i += 1) {
      list.push({ thread_id: `t-${i}`, summary: `第 ${i} 个会话：关于天气与搜索的讨论`, message_count: i * 2 });
    }
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(list));
    return;
  }

  if (url.pathname === '/api/chat/stream') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      'X-Accel-Buffering': 'no',
      Connection: 'keep-alive',
    });
    const send = (obj) => res.write(`data: ${JSON.stringify(obj)}\n\n`);
    send({ type: 'conversation', thread_id: 't-test' });
    let i = 0;
    const timer = setInterval(() => {
      if (i >= CHUNKS.length) {
        clearInterval(timer);
        send({ type: 'done' });
        res.end();
        return;
      }
      send({ type: 'assistant_delta', content: (i ? '\n\n' : '') + CHUNKS[i] });
      i += 1;
    }, 55);
    req.on('close', () => clearInterval(timer));
    return;
  }

  res.writeHead(404);
  res.end('not found');
}

// 任何一条请求出错都只影响它自己：返回 500，进程继续。
// 之前 `/original` 缺快照抛 ENOENT，直接把 server 带崩，后面所有用例
// 都变成连着失败的假象，掩盖了真正的问题。
const server = http.createServer((req, res) => {
  try {
    handle(req, res);
  } catch (error) {
    console.error(`${req.method} ${req.url} -> ${error.message}`);
    if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('mock server error');
  }
});

server.listen(PORT, '127.0.0.1', () => console.log(`mock server on http://127.0.0.1:${PORT}`));
