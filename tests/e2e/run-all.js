// e2e 的统一入口：起 mock 服务、按顺序跑所有 e2e-*.js、汇总退出码。
//
// 为什么需要它：这些脚本以前各自为政，没有"一条命令全跑"的入口，
// 也没有任何一个自动化的地方会跑它们——于是 mock 少了 /static 处理、
// 页面拿不到脚本这种「所有用例一起坏」的事故能潜伏很多天。
// 有了这个入口，CI 才有东西可跑（见 .github/workflows/ci.yml 的 e2e job）。
//
// 用法：node tests/e2e/run-all.js [--only e2e-katex,e2e-layout]
const { spawn, spawnSync } = require('child_process');
const http = require('http');
const path = require('path');
const fs = require('fs');

const ROOT = path.join(__dirname, '..', '..');
const MOCKS = [
  { script: 'mock-server.js', port: 8766 },
  { script: 'mock-conversations.js', port: 8768 },
];
// 全量清单。想跳过哪个就在这里注释掉，不要靠记忆挑着跑。
const SUITE = [
  'e2e-app-modules.js',
  'e2e-katex.js',
  'e2e-guest-cookie.js',
  'e2e-sidebar.js',
  'e2e-mobile-drawer.js',
  'e2e-mobile-modal.js',
  'e2e-scrollbutton.js',
  'e2e-conversation-menu.js',
  'e2e-cite-popover.js',
  'e2e-library-mobile.js',
  'e2e-favicon.js',
  'e2e-layout.js',
  'e2e-wheel.js',
  'e2e-sidebar-wheel.js',
  'e2e-scroll.js',
];

const only = (() => {
  const i = process.argv.indexOf('--only');
  return i !== -1 ? process.argv[i + 1].split(',') : null;
})();

function waitReady(port, timeoutMs = 15000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const probe = () => {
      const req = http.get({ host: '127.0.0.1', port, path: '/', timeout: 1500 }, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (Date.now() - started > timeoutMs) reject(new Error(`mock 服务 ${port} 起不来`));
        else setTimeout(probe, 300);
      });
      req.on('timeout', () => req.destroy(new Error('timeout')));
    };
    probe();
  });
}

(async () => {
  // node_modules 必须在，否则 require('playwright-core') 会在每个脚本里报同一个错。
  if (!fs.existsSync(path.join(ROOT, 'node_modules', 'playwright-core'))) {
    console.error('缺依赖：先 npm install（见 package.json 的 devDependencies）');
    process.exit(1);
  }

  const children = [];
  for (const mock of MOCKS) {
    const child = spawn(process.execPath, [path.join(__dirname, mock.script)], {
      cwd: ROOT,
      stdio: ['ignore', 'ignore', 'pipe'],
    });
    child.stderr.on('data', (chunk) => process.stderr.write(`[mock:${mock.port}] ${chunk}`));
    children.push(child);
  }

  const failures = [];
  let exitCode = 0;
  try {
    await Promise.all(MOCKS.map((m) => waitReady(m.port)));
    console.log(`mock 服务就绪（${MOCKS.map((m) => m.port).join(' / ')}），共 ${SUITE.length} 个脚本\n`);

    for (const name of SUITE) {
      if (only && !only.includes(name.replace(/\.js$/, '')) && !only.includes(name)) continue;
      const result = spawnSync(process.execPath, [path.join(__dirname, name)], {
        cwd: ROOT,
        encoding: 'utf8',
        timeout: 180000,
      });
      const failed = result.status !== 0 || result.error;
      console.log(`${failed ? 'FAIL' : 'PASS'}  ${name}${failed && result.status !== 0 ? `  (exit ${result.status})` : ''}${failed && result.error ? `  ${result.error.message}` : ''}`);
      if (failed) {
        failures.push(name);
        // 失败时把输出带上——排查时最有用的是它自己的报错，不是汇总。
        (result.stdout || '').split('\n').filter((l) => /FAIL|Error|error/i.test(l)).slice(0, 8)
          .forEach((l) => console.log(`      ${l}`));
      }
    }
  } catch (error) {
    console.error(error.message);
    exitCode = 1;
  } finally {
    children.forEach((child) => child.kill());
  }

  const ran = only ? '（--only 子集）' : '';
  if (!exitCode) {
    console.log(failures.length
      ? `\n${failures.length} 个脚本失败${ran}：${failures.join(', ')}`
      : `\n全部通过${ran}`);
    exitCode = failures.length ? 1 : 0;
  }
  process.exit(exitCode);
})();
