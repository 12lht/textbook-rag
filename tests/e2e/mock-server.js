const http = require('http');
const fs = require('fs');
const path = require('path');

const FRONTEND = 'D:/agent/frontend/index.html';
const ORIGINAL = 'D:/agent/.workbuddy/tmp/index.original.html';
const PORT = 8766;

const SENT = '这是一段用于验证流式输出时滚动跟随行为的文本，它会不断追加到当前消息中，使页面持续变长。';
const CHUNKS = [];
for (let i = 1; i <= 200; i += 1) CHUNKS.push(`第 ${i} 段。${SENT}`);

const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');

  if (url.pathname === '/' || url.pathname === '/library') {
    const file = url.pathname === '/library'
      ? 'D:/agent/frontend/library.html'
      : FRONTEND;
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(fs.readFileSync(file));
    return;
  }

  if (url.pathname === '/original') {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(fs.readFileSync(ORIGINAL));
    return;
  }

  if (url.pathname === '/api/auth/me') {
    // e2e 假身份：knowledge 库页有登录门，不给 200 就整页被 gate 挡住
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ account: 'e2e-user', guest: false }));
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
});

server.listen(PORT, '127.0.0.1', () => console.log(`mock server on http://127.0.0.1:${PORT}`));
