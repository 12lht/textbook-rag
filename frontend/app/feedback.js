// 反馈：提示行、忙碌态、滚动跟随
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { $, HINT_IDLE, state } from './core.js';

// 本模块用到的 DOM 节点
const chat = $('#chat');
const input = $('#input');
const send = $('#send');
const hint = $('.hint');
const scrollBottomButton = $('#scrollBottom');

export function setHint(text, isError = false) {
  hint.textContent = text || HINT_IDLE;
  hint.classList.toggle('error', Boolean(text) && isError);
}

export function setBusy(busy) {
  send.disabled = busy;
  input.disabled = busy;
  state.busy = busy;
  document.querySelectorAll('.rewind').forEach((button) => { button.disabled = busy; });
}

export function setStatus(text, busy = false) {
  setBusy(busy);
  setHint(text);
}

export function isNearBottom(threshold = 120) {
  return chat.scrollHeight - chat.scrollTop - chat.clientHeight <= threshold;
}

export function scrollToBottom() {
  chat.scrollTop = chat.scrollHeight;
}

export function updateScrollBottomButton() {
  scrollBottomButton.classList.toggle('visible', !isNearBottom());
}

scrollBottomButton.onclick = () => {
  state.autoFollow = true;
  scrollToBottom();
  updateScrollBottomButton();
};

chat.addEventListener('scroll', () => {
  const currentScrollTop = chat.scrollTop;
  if (currentScrollTop < state.lastScrollTop - 2 && !isNearBottom()) {
    state.autoFollow = false;
  } else if (isNearBottom()) {
    state.autoFollow = true;
  }
  state.lastScrollTop = currentScrollTop;
  updateScrollBottomButton();
}, { passive: true });
