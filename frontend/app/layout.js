// 外壳：移动端抽屉、聚焦徽标
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { $, state } from './core.js';

// 本模块用到的 DOM 节点
const scopeBadge = $('#scopeBadge');
const sidebar = document.querySelector('.sidebar');
const backdrop = $('#sidebarBackdrop');

export const openSidebar = () => { sidebar.classList.add('open'); backdrop.classList.add('show'); };
export const closeSidebar = () => { sidebar.classList.remove('open'); backdrop.classList.remove('show'); };
export const closeSidebarIfMobile = () => { if (window.matchMedia('(max-width: 760px)').matches) closeSidebar(); };

export function renderScopeBadge() {
  // 聚焦徽标：显示当前在哪本书里，点一下退出（清 URL 参数，恢复全书检索）。
  if (!state.scope) { scopeBadge.hidden = true; return; }
  scopeBadge.hidden = false;
  scopeBadge.textContent = `聚焦：《${state.scopeName}》 ✕`;
}

{
  const params = new URLSearchParams(window.location.search);
  if (params.get('scope')) {
    state.scope = params.get('scope');
    state.scopeName = params.get('scope_name') || '聚焦教材';
  }
}

$('#menuBtn').onclick = openSidebar;
backdrop.onclick = closeSidebar;

scopeBadge.onclick = () => {
  state.scope = null;
  state.scopeName = '';
  window.history.replaceState({}, '', window.location.pathname);
  renderScopeBadge();
};

renderScopeBadge();
