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

window.fetch = () => new Promise(() => {});
const store = new Map();
window.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};
window.setTimeout = () => 0;
window.requestAnimationFrame = () => 0;

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

const addReasoning = window.addReasoning;
const appendReasoning = window.appendReasoning;
const addMessage = window.addMessage;
if (typeof addReasoning !== 'function') {
  console.log('[FAIL] addReasoning 不是全局函数，后面的检查没法做');
  process.exit(1);
}

const node = addMessage('assistant', '');
check(Boolean(node.traceList), 'addMessage 产出的节点带 traceList');
check(
  node.trace.querySelector('summary').textContent === '模型思考',
  '外层折叠面板标题是「模型思考」',
  JSON.stringify(node.trace.querySelector('summary').textContent),
);

const box = addReasoning(node);
check(box !== null && box !== undefined, 'addReasoning 返回了块');
check(box.tagName === 'DETAILS', '块是 <details>（可折叠）', box.tagName);
check(box.className === 'trace-reasoning', '块带上了 trace-reasoning 类名', box.className);
check(node.trace.hidden === false, '外层「模型思考」面板被自动展开');
check(node.traceList.contains(box), '块被挂进了 trace-list');
check(box.summary?.tagName === 'SUMMARY', '块里有 summary');
check(box.body?.className === 'trace-reasoning-body', '块里有正文容器');

appendReasoning(box, '我先看看用户');
appendReasoning(box, '问的是什么。');
check(box.body.textContent === '我先看看用户问的是什么。', '分片按顺序拼起来了', JSON.stringify(box.body.textContent));
check(box.chars === 12, '字数统计正确', `${box.chars}`);
check(box.summary.textContent.includes('12 字'), 'summary 上显示实时字数', box.summary.textContent);
check(box.summary.textContent.includes('我先看看用户'), 'summary 上带开头预览', box.summary.textContent);

appendReasoning(box, '很长的一段思考'.repeat(30));
check(box.summary.textContent.length < 80, 'summary 没有随思维链一起变长', `${box.summary.textContent.length} 字`);
check(box.summary.textContent.includes('…'), '内容被截断时有省略号');
check(box.chars === 12 + 7 * 30, '长文本的字数仍然准确', `${box.chars}`);
check(box.body.textContent.length === 12 + 7 * 30, '正文容器里是完整的思维链');

const histNode = addMessage(
  'assistant',
  '这是一个回答。',
  false,
  [
    { kind: 'reasoning', text: '历史里的思考过程。' },
    { kind: 'call', tool: 'get_current_time', args: { timezone: 'Asia/Shanghai' } },
    { kind: 'result', tool: 'get_current_time' },
  ],
);
const reasonings = histNode.traceList.querySelectorAll('details.trace-reasoning');
const events = histNode.traceList.querySelectorAll('div.trace-event');
check(reasonings.length === 1, '历史里渲染出 1 个思维链块', `${reasonings.length}`);
check(reasonings[0].body.textContent === '历史里的思考过程。', '历史思维链内容正确');
check(events.length === 2, '工具调用 / 结果仍然各占一行', `${events.length}`);
check(
  events[0].textContent.includes('调用工具：get_current_time'),
  '工具调用那行没被思维链顶掉',
  events[0].textContent,
);
check(
  !histNode.traceList.textContent.includes('undefined'),
  '没有渲染出 undefined（kind 分派没漏分支）',
);

const addToolResult = window.addToolResult;
check(typeof addToolResult === 'function', 'addToolResult 是全局函数');

const okNode = addMessage('assistant', '', false, [
  { kind: 'call', tool: 'web_search', args: { query: 'x' } },
  { kind: 'result', tool: 'web_search', status: 'success' },
]);
const badNode = addMessage('assistant', '', false, [
  { kind: 'call', tool: 'fetch_webpage', args: { url: 'https://x.invalid' } },
  { kind: 'result', tool: 'fetch_webpage', status: 'error' },
]);
const oldNode = addMessage('assistant', '', false, [{ kind: 'result', tool: 'get_current_time' }]);

const okLine = okNode.traceList.querySelectorAll('div.trace-event')[1];
const badLine = badNode.traceList.querySelectorAll('div.trace-event')[1];
const oldLine = oldNode.traceList.querySelector('div.trace-event');

check(okLine?.textContent === '工具完成：web_search', 'status=success → 「工具完成」', okLine?.textContent);
check(!okLine?.classList.contains('trace-failed'), '成功那行没有失败样式');
check(badLine?.textContent === '工具失败：fetch_webpage', 'status=error → 「工具失败」', badLine?.textContent);
check(badLine?.classList.contains('trace-failed'), '失败那行带 trace-failed 类（会被标红）');
check(oldLine?.textContent === '工具完成：get_current_time', '没有 status 字段时按成功处理（兼容老数据）', oldLine?.textContent);
check(!oldLine?.classList.contains('trace-failed'), '没有 status 时不会被误标成失败');

const liveNode = addMessage('assistant', '');
addToolResult(liveNode, 'web_search', 'error');
check(
  liveNode.traceList.querySelector('div.trace-event')?.textContent === '工具失败：web_search',
  '实时流路径（addToolResult）也按 status 分叉',
  liveNode.traceList.querySelector('div.trace-event')?.textContent,
);

console.log(`\n失败项：${failures}`);
process.exit(failures ? 1 : 0);
