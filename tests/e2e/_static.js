// 给 e2e 的 mock server 提供 `/static/*` —— 与后端 `app.mount("/static", frontend)` 对齐。
//
// 为什么需要：前端拆成 index.html + app.js + app.css 之后，mock server 只回 HTML
// 是不够的，`/static/app.js` 会 404，页面拿不到脚本，所有断言都在对着空壳跑。
// 这个洞在拆分当天就存在，只是因为 e2e 不在 CI 里，一直没人发现。
const fs = require('fs');
const path = require('path');

const FRONTEND = path.join(__dirname, '..', '..', 'frontend');

const TYPES = {
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
};

/** 命中 `/static/*` 就自己回掉并返回 true，其余情况返回 false 交给调用方。 */
function serveStatic(req, res) {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (!url.pathname.startsWith('/static/')) return false;

  const target = path.resolve(FRONTEND, decodeURIComponent(url.pathname.slice('/static/'.length)));
  // 只允许读 frontend/ 内部，挡住 `../` 跳出仓库。
  if (target !== FRONTEND && !target.startsWith(FRONTEND + path.sep)) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('forbidden');
    return true;
  }

  let body;
  try {
    body = fs.readFileSync(target);
  } catch {
    res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('not found');
    return true;
  }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(target).toLowerCase()] || 'application/octet-stream' });
  res.end(body);
  return true;
}

module.exports = { serveStatic, FRONTEND };
