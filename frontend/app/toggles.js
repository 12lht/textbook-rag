// 开关：深度思考 / 联网搜索，存浏览器不存会话
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch } from './api.js';
import { $, TOGGLE_KEY, toggles } from './core.js';

// 本模块用到的 DOM 节点
const input = $('#input');

export function restoreToggles() {
  try {
    const saved = JSON.parse(localStorage.getItem(TOGGLE_KEY) || '{}');
    // 只认布尔值：存过别的东西（比如旧版本存的是字符串）就当没存过，
    // 照默认「都开」走。宁可丢掉一次选择，也不要让按钮显示成 undefined。
    if (typeof saved.reasoning === 'boolean') toggles.reasoning = saved.reasoning;
    if (typeof saved.web_search === 'boolean') toggles.web_search = saved.web_search;
  } catch (error) {
    // 坏 JSON 不影响使用，用默认值
  }
}

export function saveToggles() {
  localStorage.setItem(TOGGLE_KEY, JSON.stringify(toggles));
}

export function paintToggles() {
  [['#toggleReasoning', 'reasoning'], ['#toggleWeb', 'web_search']].forEach(([selector, key]) => {
    const button = $(selector);
    button.classList.toggle('active', toggles[key]);
    button.setAttribute('aria-pressed', String(toggles[key]));
  });
}

export function bindToggle(selector, key) {
  $(selector).onclick = () => {
    toggles[key] = !toggles[key];
    saveToggles();
    paintToggles();
    input.focus({ preventScroll: true });
  };
}

export async function loadCapabilities() {
  let caps;
  try {
    const response = await apiFetch('/api/capabilities');
    if (!response.ok) return;
    caps = await response.json();
  } catch (error) {
    return;  // 拿不到就按「都可用」处理，不要因为一个能力查询挡住两个按钮
  }
  if (!caps.reasoning_toggleable) {
    $('#toggleReasoning').disabled = true;
    $('#toggleReasoning').title = '当前模型不支持关闭深度思考';
  }
  if (!caps.web_search_available) {
    // 同时把开关拨到关：工具没配 key 时留在工具表里只是让模型白调一次，
    // 拿到一句「搜索服务未配置」。摘掉更干净。
    toggles.web_search = false;
    $('#toggleWeb').disabled = true;
    $('#toggleWeb').title = '服务端未配置搜索服务，联网不可用';
  }
  paintToggles();
}
