import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';

const require = createRequire(
  'C:/Users/13526/.workbuddy/binaries/node/workspace/package.json',
);
const { parseHTML } = require('linkedom');

let ok = true;
const check = (name, cond, detail = '') => {
  console.log((cond ? '  OK  ' : '  FAIL') + ` ${name}` + (detail ? `  ${detail}` : ''));
  if (!cond) ok = false;
};

// --- index.html：徽标元素存在 + 脚本语法合法 + state 解析 URL ---
const HTML = readFileSync('D:/agent/frontend/index.html', 'utf8');
check('scopeBadge 元素存在', HTML.includes('id="scopeBadge"'));

const script = [...HTML.matchAll(/<script>([\s\S]*?)<\/script>/g)].pop()[1];
try {
  new vm.Script(script);
  check('index.html 脚本语法合法', true);
} catch (error) {
  check('index.html 脚本语法合法', false, String(error));
}
check('发消息带 scope_category_id', script.includes('scope_category_id: state.scope'));
check('读取 URL 的 scope 参数', script.includes("params.get('scope')"));
check('退出聚焦清 URL', script.includes('history.replaceState'));

// --- library.html：分类行有「对话」入口 ---
const LIB = readFileSync('D:/agent/frontend/library.html', 'utf8');
check('library 有进入对话入口', LIB.includes("window.location.href = `/?scope="));

console.log();
console.log('结果:', ok ? '通过' : '有失败项');
process.exit(ok ? 0 : 1);
