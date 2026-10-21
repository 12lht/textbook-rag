// 前端两个开关的 DOM 测试：把 index.html 里那段 script 真的跑起来。
//
// 要钉住的不是「按钮画出来了没有」，而是三件会静默出错的事：
//   1. 点一下状态真的翻了，并且**记住了**（刷新后还是上次的选择）；
//   2. 发消息时这两个值真的跟着请求体走了（拼错字段名后端会当没传，
//      表现就是「按钮点了没反应」，而控制台一点错都不报）；
//   3. 服务端说某项不可用时，按钮真的被置灰——不然就是个摆设。
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(
  'C:/Users/13526/.workbuddy/binaries/node/workspace/package.json',
);
const { parseHTML } = require('linkedom');

const HTML = readFileSync('D:/agent/frontend/index.html', 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const { window, document } = parseHTML(HTML);
globalThis.document = document;

const store = new Map();
window.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
// 页面脚本里的定时器一律钉死（不然会有动画和轮询在后台跑）。
// 但**测试自己**要用真的定时器来等微任务跑完——把 setTimeout 全换成空函数之后，
// 再用 setTimeout 等待就会永远不落定（第一次就是这么卡住的）。
const realSetTimeout = globalThis.setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 5));
window.setTimeout = () => 0;
window.requestAnimationFrame = () => 0;

// fetch 桩：按 URL 分派。
//   /api/capabilities → 返回可控的能力（用来测置灰）
//   /api/chat/stream  → 抓住请求体，然后给一个空流（本测试只关心请求体）
//   其余              → 空响应。不能「永不落定」：sendMessage 收尾时会拉一次会话列表，
//                       那个 promise 不落定的话 setBusy(false) 永远执行不到，
//                       第二次发送就被 busy 挡住（第一次就是这么失败的）。
const captured = [];
let capabilities = { reasoning_toggleable: true, web_search_available: true };
window.fetch = (url, options = {}) => {
  if (String(url).includes('/api/capabilities')) {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve(capabilities),
    });
  }
  if (String(url).includes('/api/chat/stream')) {
    captured.push(JSON.parse(options.body));
    const stream = new window.ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n'));
        controller.close();
      },
    });
    return Promise.resolve({ ok: true, status: 200, body: stream });
  }
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve([]) });
};

vm.createContext(window);
try {
  vm.runInContext(script, window);
} catch (error) {
  console.log(`[FAIL] 页面脚本执行就抛了：${error.message}`);
  process.exit(1);
}
console.log('[ok]   页面脚本在真 DOM 里执行成功');

let failures = 0;
function check(condition, label, extra = '') {
  if (condition) {
    console.log(`  [ok]   ${label}`);
  } else {
    console.log(`  [FAIL] ${label}${extra ? ` —— ${extra}` : ''}`);
    failures += 1;
  }
}

const reasoningBtn = document.querySelector('#toggleReasoning');
const webBtn = document.querySelector('#toggleWeb');

// ---------- 1. 按钮存在且默认是开的 ----------
check(Boolean(reasoningBtn), '深度思考按钮存在');
check(Boolean(webBtn), '联网搜索按钮存在');
check(reasoningBtn.classList.contains('active'), '深度思考默认是开的', reasoningBtn.className);
check(webBtn.classList.contains('active'), '联网搜索默认是开的', webBtn.className);
check(reasoningBtn.getAttribute('aria-pressed') === 'true', 'aria-pressed 跟着状态走');

// ---------- 2. 点一下：状态翻 + 记住 ----------
reasoningBtn.dispatchEvent(new window.Event('click'));
check(!reasoningBtn.classList.contains('active'), '点一下深度思考 → 变成关', reasoningBtn.className);
check(reasoningBtn.getAttribute('aria-pressed') === 'false', 'aria-pressed 也翻了');
const saved = JSON.parse(store.get('agent.toggles') || '{}');
check(saved.reasoning === false, '关掉的状态被写进了 localStorage', JSON.stringify(saved));
check(saved.web_search === true, '没动过的那个按钮仍然是 true', JSON.stringify(saved));

// 再点回来
reasoningBtn.dispatchEvent(new window.Event('click'));
check(reasoningBtn.classList.contains('active'), '再点一下 → 又变回开');

// ---------- 3. 发消息时两个值真的进了请求体 ----------
const input = document.querySelector('#input');
input.value = '你好';
document.querySelector('#composer').dispatchEvent(new window.Event('submit'));

await tick();

check(captured.length === 1, '发出了一次聊天请求', `${captured.length} 次`);
const body = captured[0] || {};
check('reasoning' in body, '请求体里有 reasoning 字段', JSON.stringify(Object.keys(body)));
check('web_search' in body, '请求体里有 web_search 字段', JSON.stringify(Object.keys(body)));
check(body.reasoning === true, 'reasoning 传的是当前状态（此时为 true）', String(body.reasoning));
check(body.web_search === true, 'web_search 传的是当前状态（此时为 true）', String(body.web_search));

// 关掉联网再发一次，确认传的是 false——只测「字段存在」是不够的，
// 写死成 true 也能通过上面那几条。
webBtn.dispatchEvent(new window.Event('click'));
input.value = '你好';
document.querySelector('#composer').dispatchEvent(new window.Event('submit'));
await tick();
check(captured.length === 2, '第二次请求也发了', `${captured.length} 次`);
check(captured[1]?.web_search === false, '关掉联网后请求体里是 false', String(captured[1]?.web_search));

// ---------- 4. 服务端说不可用 → 置灰 ----------
capabilities = { reasoning_toggleable: false, web_search_available: false };
await window.loadCapabilities();
check(reasoningBtn.disabled === true, '不支持关思考 → 深度思考按钮置灰');
check(webBtn.disabled === true, '没配搜索服务 → 联网按钮置灰');
check(!webBtn.classList.contains('active'), '不可用的联网同时被拨到关', webBtn.className);
check(webBtn.title.includes('未配置'), '置灰的按钮说了为什么', webBtn.title);

console.log(`\n失败项：${failures}`);
process.exit(failures ? 1 : 0);
