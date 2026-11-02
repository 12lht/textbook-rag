// 引用小窗：点角标弹出原文与页码
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { $ } from './core.js';
import { beautifyMath, buildAssetMap, decorateCiteCodes, inlineAssets, renderMarkdown, sourceMetaText } from './render.js';

// 本模块用到的 DOM 节点
const messages = $('#messages');
const chat = $('#chat');

export function hideCitePopover() {
  const old = document.getElementById('citePopover');
  if (old) old.remove();
  document.querySelectorAll('sup.cite.active').forEach((badge) => badge.classList.remove('active'));
}

export function showCitePopover(badge, item) {
  hideCitePopover();
  const pop = document.createElement('div');
  pop.id = 'citePopover';
  // class 必须显式挂上：样式全在 .cite-popover 上，漏了它小窗就是一块
  // 没有定位的静态内容，会被塞到页面末尾（视口外）——看起来像「点了没反应」。
  pop.className = 'cite-popover';
  pop._badge = badge;
  const head = document.createElement('div');
  head.className = 'head';
  if (item.code) {
    const badgeEl = document.createElement('span');
    badgeEl.className = 'cite-code';
    badgeEl.textContent = item.code;
    head.appendChild(badgeEl);
  }
  const meta = document.createElement('span');
  meta.className = 'source-meta';
  meta.textContent = sourceMetaText(item);
  head.appendChild(meta);
  pop.appendChild(head);
  const text = typeof item.content === 'string' ? item.content.trim() : '';
  if (text) {
    const body = document.createElement('div');
    body.className = 'body';
    // 实时条目的图引用还是占位文件名，用条目自带的签名 URL 换掉；
    // 历史条目的图已被服务端内联成真 URL，这里原样通过。
    body.innerHTML = renderMarkdown(inlineAssets(decorateCiteCodes(text), buildAssetMap([item])));
    // MinerU 的公式是单 $..$，要靠 KaTeX 才成公式——正文章节渲染完都会走
    // beautifyMath，小窗以前漏了这一步，原文里的公式一直是裸 LaTeX。
    beautifyMath(body);
    pop.appendChild(body);
  } else if (item.asset_url) {
    // MinerU 的 figure 块可能是纯图无题注，content 为空：直接把图放出来，
    // 否则点开是一块空白，等于「引用了但看不到东西」。
    const body = document.createElement('div');
    body.className = 'body';
    const img = document.createElement('img');
    img.src = item.asset_url;
    img.alt = `第 ${item.page_start || '?'} 页的插图`;
    body.appendChild(img);
    pop.appendChild(body);
  }
  document.body.appendChild(pop);
  // 先挂上 DOM 量出尺寸再定位：默认弹在角标下方，放不下就翻到上方，
  // 左右夹在视口内。
  const rect = badge.getBoundingClientRect();
  const width = pop.offsetWidth;
  const height = pop.offsetHeight;
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
  let top = rect.bottom + 8;
  if (top + height > window.innerHeight - 8) {
    top = Math.max(8, rect.top - height - 8);
  }
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
  badge.classList.add('active');
}

export function toggleCitePopover(badge) {
  const contentNode = badge.closest('.message-content');
  const item = contentNode?._citations?.[badge.dataset.code];
  if (!item) return; // 引用事件还没到（流式期间），点了没东西可看
  const existing = document.getElementById('citePopover');
  if (existing && existing._badge === badge) {
    hideCitePopover();
    return;
  }
  showCitePopover(badge, item);
}

messages.addEventListener('click', (event) => {
  const badge = event.target.closest('sup.cite');
  if (badge) toggleCitePopover(badge);
});

document.addEventListener('mousedown', (event) => {
  if (event.target.closest('#citePopover') || event.target.closest('sup.cite')) return;
  hideCitePopover();
}, true);

chat.addEventListener('scroll', () => hideCitePopover(), { passive: true });

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') hideCitePopover();
});
