// 共享内核：视图状态、跨模块可变状态、本地存储键、通用工具
//
// 由 frontend/app.js 拆出。这里是所有模块共用的那一层，其余模块都从这儿取。


// 跨模块可变的状态。
//
// ES module 导出的绑定是只读的——A 模块里写 `token = x` 会直接抛 TypeError，
// 所以凡是要被别的模块改的值都挂在同一个对象上。副作用是「谁改了 token」
// 变得可搜：全项目只有 store.token 这一种写法。
export const store = {
  // 令牌是服务端签发、存在 localStorage 的唯一凭证，前端不自己声称「我是谁」。
  // identity 只用于界面展示，权限判断在服务端，别拿它当安全边界。
  token: readToken(),
  identity: null,
  authMode: 'login',
  // 注册要不要邀请码。null = 还没问过服务端——这是服务端的事实，前端猜不出来。
  needsInvite: null,
  // 401 只处理一次：一次页面操作可能并发打好几个请求，全都 401 的话
  // 会重复清状态、重复弹提示。处理期间把它挡住。
  handlingUnauthorized: false,
  // 会话右键菜单当前指向的那一行。
  menuTarget: null,
  memoryBusy: false,   // 一次只放一个请求出去，防连点
  inviteBusy: false,
};

export const state = { threadId: null, busy: false, autoFollow: true, lastScrollTop: 0, location: null, locationLabel: '', scope: null, scopeName: '', pending: [] };
export const $ = (selector) => document.querySelector(selector);
export const HINT_IDLE = 'Enter 发送 · Shift + Enter 换行';
export const LOCATION_KEY = 'agent.location';
export const TOKEN_KEY = 'agent.token';
export const TOGGLE_KEY = 'agent.toggles';
export const toggles = { reasoning: true, web_search: true };

export function readToken() {
  try { return localStorage.getItem(TOKEN_KEY) || null; } catch (error) { return null; }
}

export function writeToken(value) {
  store.token = value;
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch (error) {
    // 无痕模式下 localStorage 会抛错。记不住令牌的后果是「刷新后退出登录」，
    // 本次会话仍然可用，不值得打断用户。
  }
}
