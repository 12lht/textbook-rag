// 入口：把各模块接起来，启动时拉身份与会话
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { refreshIdentity } from './auth.js';
import { hideCitePopover, showCitePopover } from './cites.js';
import { loadConversations, newConversation, selectConversation } from './conversations.js';
import { $, state } from './core.js';
import { renderLocateButton, restoreLocation } from './location.js';
import { addMessage } from './messages.js';
import { bindToggle, loadCapabilities, paintToggles, restoreToggles } from './toggles.js';

// 这些模块的事件绑定挂在它们自己的顶层，import 才会执行。
import './feedback.js';
import './layout.js';
import './composer.js';
import './memory.js';
import './invite.js';

// 本模块用到的 DOM 节点
const input = $('#input');

$('#newChat').onclick = newConversation;
document.querySelectorAll('.suggestion').forEach((button) => { button.onclick = () => { input.value = button.dataset.prompt; input.focus(); }; });
renderLocateButton();
restoreLocation();
restoreToggles();
bindToggle('#toggleReasoning', 'reasoning');
bindToggle('#toggleWeb', 'web_search');
paintToggles();

(async () => {
  await refreshIdentity();
  await loadConversations();
  await loadCapabilities();
})();

// e2e 的契约：下面这几个名字要能从页面全局拿到。页面本身没有任何内联 onclick，
// 这是唯一一处主动往 window 上挂东西的地方，改这里等于改测试契约：
//   state                          —— 读 autoFollow / threadId / busy 这些运行态
//   addMessage                     —— 造一条指定内容的消息，验证渲染
//   showCitePopover / hideCitePopover —— 直接驱动引用小窗，不必点角标
//   selectConversation             —— 切到指定会话
Object.assign(window, { state, addMessage, showCitePopover, hideCitePopover, selectConversation });
