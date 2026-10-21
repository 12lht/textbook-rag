// 支持 GET / PATCH / DELETE /api/conversations 的假后端，用于验证右键菜单。
// 端口 8768。状态在内存里，可用 POST /__reset 重置。
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = 8768;
const INDEX = path.join(__dirname, '..', '..', 'frontend', 'index.html');

const SEED = [
  { thread_id: 't-aaa11111', summary: '北京今天的天气', message_count: 4 },
  { thread_id: 't-bbb22222', summary: '检索最近的科技新闻', message_count: 2 },
  { thread_id: 't-ccc33333', summary: '新会话（暂无消息）', message_count: 0 },
];

let conversations = [];
let requests = [];

function reset() {
  conversations = SEED.map((item) => ({ ...item }));
  requests = [];
}

function readBody(request) {
  return new Promise((resolve) => {
    let raw = '';
    request.on('data', (chunk) => { raw += chunk; });
    request.on('end', () => {
      try { resolve(raw ? JSON.parse(raw) : {}); } catch (error) { resolve({}); }
    });
  });
}

function send(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
  response.end(body);
}

reset();

http.createServer(async (request, response) => {
  const url = new URL(request.url, `http://127.0.0.1:${PORT}`);
  const { pathname } = url;

  if (pathname === '/' ) {
    response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    response.end(fs.readFileSync(INDEX));
    return;
  }

  if (pathname === '/__reset' && request.method === 'POST') {
    reset();
    send(response, 200, { status: 'reset' });
    return;
  }

  if (pathname === '/__requests') {
    send(response, 200, requests);
    return;
  }

  if (pathname === '/api/conversations' && request.method === 'GET') {
    send(response, 200, conversations);
    return;
  }

  if (pathname === '/api/conversations' && request.method === 'POST') {
    const created = { thread_id: `t-${Math.random().toString(16).slice(2, 10)}`, summary: '新会话（暂无消息）', message_count: 0 };
    conversations.unshift(created);
    send(response, 200, { thread_id: created.thread_id, summary: created.summary });
    return;
  }

  const messagesMatch = pathname.match(/^\/api\/conversations\/([^/]+)\/messages$/);
  if (messagesMatch && request.method === 'GET') {
    send(response, 200, { thread_id: messagesMatch[1], messages: [] });
    return;
  }

  const singleMatch = pathname.match(/^\/api\/conversations\/([^/]+)$/);
  if (singleMatch) {
    const threadId = decodeURIComponent(singleMatch[1]);
    const index = conversations.findIndex((item) => item.thread_id === threadId);

    if (request.method === 'PATCH') {
      const body = await readBody(request);
      requests.push({ method: 'PATCH', threadId, body });
      if (index === -1) { send(response, 404, { detail: '会话不存在' }); return; }
      const title = String(body.title || '').split(/\s+/).filter(Boolean).join(' ').slice(0, 30);
      if (!title) { send(response, 400, { detail: '会话标题不能为空' }); return; }
      conversations[index].summary = title;
      send(response, 200, { thread_id: threadId, summary: title });
      return;
    }

    if (request.method === 'DELETE') {
      requests.push({ method: 'DELETE', threadId });
      if (index === -1) { send(response, 404, { detail: '会话不存在' }); return; }
      conversations.splice(index, 1);
      send(response, 200, { thread_id: threadId, status: 'deleted' });
      return;
    }
  }

  send(response, 404, { detail: 'not found' });
}).listen(PORT, '127.0.0.1', () => {
  console.log(`mock conversations server on http://127.0.0.1:${PORT}`);
});
