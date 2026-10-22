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

for (let i = 0; i < 5; i += 1) await tick();
check('聊天页不再请求 /api/documents',
  !calls.some((url) => url === '/api/documents'),
  calls.join(' '));

check('记忆入口还在', Boolean(document.querySelector('#memoryEntry')));
check('邀请入口还在', Boolean(document.querySelector('#inviteEntry')));

console.log(failures ? `\n失败 ${failures} 项` : '\n全部通过');
process.exit(failures ? 1 : 0);
