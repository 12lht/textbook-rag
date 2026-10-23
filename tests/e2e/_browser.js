// 浏览器可执行文件的解析：不写死盘符，换机器不用改脚本。
// 顺序：CHROME_PATH 环境变量 > playwright 缓存里已装的 chromium（取版本最高）> playwright 自报路径。
const fs = require('fs');
const os = require('os');
const path = require('path');
const { chromium } = require('playwright-core');

const CACHE = process.platform === 'win32'
  ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'ms-playwright')
  : path.join(os.homedir(), '.cache', 'ms-playwright');

const LAYOUT = {
  win32: ['chrome-win64/chrome.exe', 'chrome-win/chrome.exe'],
  darwin: ['chrome-mac/Chromium.app/Contents/MacOS/Chromium'],
};

function installed() {
  const wanted = LAYOUT[process.platform] || ['chrome-linux/chrome'];
  let dirs = [];
  try {
    dirs = fs.readdirSync(CACHE);
  } catch {
    return [];
  }
  return dirs
    .filter((name) => name.startsWith('chromium-'))
    .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]))
    .flatMap((name) => wanted.map((rel) => path.join(CACHE, name, rel)))
    .filter((full) => fs.existsSync(full));
}

module.exports = process.env.CHROME_PATH || installed()[0] || chromium.executablePath();
