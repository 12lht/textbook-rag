import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(
  import.meta.url,
);
const { parseHTML } = require('linkedom');

const HTML = readFileSync(new URL('../../frontend/index.html', import.meta.url), 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const { window, document } = parseHTML(HTML);
// linkedom 不实现 matchMedia；前端靠它区分手机/桌面，缺了会直接抛
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
globalThis.document = document;

const store = new Map();
window.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
const realSetTimeout = globalThis.setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 5));
window.setTimeout = () => 0;
window.requestAnimationFrame = () => 0;

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

check(Boolean(reasoningBtn), '深度思考按钮存在');
check(Boolean(webBtn), '联网搜索按钮存在');
check(reasoningBtn.classList.contains('active'), '深度思考默认是开的', reasoningBtn.className);
check(webBtn.classList.contains('active'), '联网搜索默认是开的', webBtn.className);
check(reasoningBtn.getAttribute('aria-pressed') === 'true', 'aria-pressed 跟着状态走');

reasoningBtn.dispatchEvent(new window.Event('click'));
check(!reasoningBtn.classList.contains('active'), '点一下深度思考 → 变成关', reasoningBtn.className);
check(reasoningBtn.getAttribute('aria-pressed') === 'false', 'aria-pressed 也翻了');
const saved = JSON.parse(store.get('agent.toggles') || '{}');
check(saved.reasoning === false, '关掉的状态被写进了 localStorage', JSON.stringify(saved));
check(saved.web_search === true, '没动过的那个按钮仍然是 true', JSON.stringify(saved));

reasoningBtn.dispatchEvent(new window.Event('click'));
check(reasoningBtn.classList.contains('active'), '再点一下 → 又变回开');

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

webBtn.dispatchEvent(new window.Event('click'));
input.value = '你好';
document.querySelector('#composer').dispatchEvent(new window.Event('submit'));
await tick();
check(captured.length === 2, '第二次请求也发了', `${captured.length} 次`);
check(captured[1]?.web_search === false, '关掉联网后请求体里是 false', String(captured[1]?.web_search));

capabilities = { reasoning_toggleable: false, web_search_available: false };
await window.loadCapabilities();
check(reasoningBtn.disabled === true, '不支持关思考 → 深度思考按钮置灰');
check(webBtn.disabled === true, '没配搜索服务 → 联网按钮置灰');
check(!webBtn.classList.contains('active'), '不可用的联网同时被拨到关', webBtn.className);
check(webBtn.title.includes('未配置'), '置灰的按钮说了为什么', webBtn.title);

console.log(`\n失败项：${failures}`);
process.exit(failures ? 1 : 0);
