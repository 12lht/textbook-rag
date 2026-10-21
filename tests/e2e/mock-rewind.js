const http = require('http');
const fs = require('fs');

const FRONTEND = 'D:/agent/frontend/index.html';
const PORT = 8767;

let threadId = 't-rewind';
let history = [];

const readBody = (req) => new Promise((resolve) => {
  let raw = '';
  req.on('data', (chunk) => { raw += chunk; });
  req.on('end', () => {
    try { resolve(JSON.parse(raw || '{}')); } catch { resolve({}); }
  });
});

const json = (res, status, payload) => {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
};

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  const path = url.pathname;

  if (req.method === 'GET' && (path === '/' || path === '/index.html')) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(fs.readFileSync(FRONTEND));
    return;
  }

  if (req.method === 'GET' && path === '/api/conversations') {
    json(res, 200, [{ thread_id: threadId, summary: '回退测试会话', message_count: history.length }]);
    return;
  }

  if (req.method === 'POST' && path === '/api/conversations') {
    threadId = `t-${Date.now().toString(36)}`;
    history = [];
    json(res, 200, { thread_id: threadId, summary: '新会话（暂无消息）' });
    return;
  }

  const messagesMatch = path.match(/^\/api\/conversations\/([^/]+)\/messages$/);
  if (req.method === 'GET' && messagesMatch) {
    json(res, 200, { thread_id: threadId, messages: history });
    return;
  }

  const rewindMatch = path.match(/^\/api\/conversations\/([^/]+)\/rewind$/);
  if (req.method === 'POST' && rewindMatch) {
    const { turn } = await readBody(req);
    const userPositions = history
      .map((message, index) => (message.role === 'user' ? index : -1))
      .filter((index) => index >= 0);
    if (typeof turn !== 'number' || turn < 0 || turn >= userPositions.length) {
      json(res, 400, { detail: '要回退的消息不存在' });
      return;
    }
    const cut = userPositions[turn];
    const removed = history.slice(cut);
    history = history.slice(0, cut);
    json(res, 200, {
      thread_id: threadId,
      removed: removed.length,
      text: removed[0].content,
      messages: history,
    });
    return;
  }

  if (req.method === 'POST' && path === '/api/chat/stream') {
    const { message } = await readBody(req);
    history.push({ role: 'user', content: message });

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    const send = (payload) => res.write(`data: ${JSON.stringify(payload)}\n\n`);
    send({ type: 'conversation', thread_id: threadId });

    const reply = `收到「${message}」。这是模拟回复。`;
    const chunks = [...reply];
    let index = 0;
    const timer = setInterval(() => {
      if (index >= chunks.length) {
        clearInterval(timer);
        history.push({ role: 'assistant', content: reply });
        send({ type: 'done' });
        res.end();
        return;
      }
      send({ type: 'assistant_delta', content: chunks[index] });
      index += 1;
    }, 20);
    req.on('close', () => clearInterval(timer));
    return;
  }

  json(res, 404, { detail: 'not found' });
});

server.listen(PORT, '127.0.0.1', () => console.log(`rewind mock server on http://127.0.0.1:${PORT}`));
