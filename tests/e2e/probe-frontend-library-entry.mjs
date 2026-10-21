// 侧栏「还原」的 DOM 测试：教材管理搬去独立页面之后，聊天页的侧栏应该
// 回到加这个功能之前的样子。
//
// 钉的是三件事，都是**不测就会悄悄退回去**的：
//   1. 侧栏里那套文档列表/上传按钮真的没了（不是隐藏——隐藏在 DOM 里还留着，
//      下次谁把它 hidden = false 就又冒出来了）；
//   2. 留了一个能点到 /library 的入口，否则功能等于被删掉了；
//   3. 聊天页**不再请求 /api/documents**。这条最要紧：只要还剩一处调用，
//      每次开聊天页都会白拉一遍全量书单（几十 KB），而且书多了会变慢。
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire('C:/Users/13526/.workbuddy/binaries/node/workspace/package.json');
const { parseHTML } = require('linkedom');

const HTML = readFileSync('D:/agent/frontend/index.html', 'utf8');
const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];

const { window, document } = parseHTML(HTML);
globalThis.document = document;

const store = new Map([['agent.token', 'tok-123']]);
window.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const realSetTimeout = globalThis.setTimeout;
const tick = () => new Promise((resolve) => realSetTimeout(resolve, 5));
window.setTimeout = () => 0;
window.requestAnimationFrame = () => 0;

const calls = [];
window.fetch = (url) => {
  calls.push(String(url));
  const json = (body) => ({ ok: true, status: 200, json: () => Promise.resolve(body) });
  if (String(url).includes('/api/auth/me')) {
    return Promise.resolve(json({
      user_id: 'u-1', account: 'taoge', guest: false, memory_enabled: true, is_owner: true,
    }));
  }
  if (String(url).includes('/api/documents')) {
    // 拉到了就说明还在调用——返回一份假书单，好让下面那条断言说得清楚。
    return Promise.resolve(json([{ doc_id: 'd1', title: '不该被拉到的书', status: 'ready' }]));
  }
  return Promise.resolve(json([]));
};

vm.createContext(window);
let failures = 0;
function check(label, condition, extra = '') {
  console.log(`${condition ? 'PASS' : 'FAIL'}  ${label}${extra ? '  ' + extra : ''}`);
  if (!condition) failures += 1;
}

try {
  vm.runInContext(script, window);
} catch (error) {
  check('页面脚本能执行', false, error.message);
  process.exit(1);
}
await tick();

check('移除 #docList', document.querySelector('#docList') === null,
  document.querySelector('#docList') ? '还在 DOM 里' : '');
check('移除 #uploadButton', document.querySelector('#uploadButton') === null);
check('移除 #fileInput', document.querySelector('#fileInput') === null);
check('移除「Textbooks · 教材库」标题',
  !HTML.includes('Textbooks · 教材库'),
  HTML.includes('Textbooks · 教材库') ? '还留着这个标题' : '');

const entry = document.querySelector('#libraryEntry');
check('留下知识库入口', Boolean(entry));
check('入口指向 /library', entry && entry.getAttribute('href') === '/library',
  entry ? entry.getAttribute('href') : '(没有入口)');
check('入口文字说清是干什么的',
  entry && entry.textContent.includes('知识库'),
  entry ? entry.textContent.trim() : '');
check('入口是链接不是按钮（可直接右键开新标签）',
  entry && entry.tagName.toLowerCase() === 'a', entry ? entry.tagName : '');

// 会话结束后再确认一次：boot 里的 refreshIdentity → loadConversations 是异步的，
// 上面只等了一个 tick，这里给足时间。
for (let i = 0; i < 5; i += 1) await tick();
check('聊天页不再请求 /api/documents',
  !calls.some((url) => url === '/api/documents'),
  calls.join(' '));

// 记忆/邀请入口是 F 域的东西，不该被这次改动连带删掉。
check('记忆入口还在', Boolean(document.querySelector('#memoryEntry')));
check('邀请入口还在', Boolean(document.querySelector('#inviteEntry')));

console.log(failures ? `\n失败 ${failures} 项` : '\n全部通过');
process.exit(failures ? 1 : 0);
