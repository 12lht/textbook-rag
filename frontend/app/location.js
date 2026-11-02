// 位置：浏览器授权优先，失败降级为手选城市
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


import { apiFetch, readError } from './api.js';
import { $, LOCATION_KEY, state } from './core.js';
import { setHint } from './feedback.js';

// 本模块用到的 DOM 节点
const locateButton = $('#locate');

export function loadStoredLocation() {
  try {
    const raw = localStorage.getItem(LOCATION_KEY);
    if (!raw) return null;
    const saved = JSON.parse(raw);
    return saved && typeof saved === 'object' && saved.location ? saved : null;
  } catch (error) {
    return null; // 存的东西坏了就当没存过，不影响使用
  }
}

export function renderLocateButton() {
  locateButton.textContent = state.locationLabel || '定位';
  locateButton.classList.toggle('set', Boolean(state.location));
  locateButton.title = state.locationLabel
    ? `当前位置：${state.locationLabel} · 点击重新设置`
    : '点击设置位置（浏览器授权，或手动输入城市）';
}

export function setLocation(location, label) {
  state.location = location;
  state.locationLabel = label || '';
  try {
    if (location) {
      localStorage.setItem(LOCATION_KEY, JSON.stringify({ location, label: state.locationLabel }));
    } else {
      localStorage.removeItem(LOCATION_KEY);
    }
  } catch (error) {
    // 隐私模式下 localStorage 会抛错。记不住不影响这次会话，忽略。
  }
  renderLocateButton();
}

export function locateByCoordinates() {
  return new Promise((resolve) => {
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const { latitude, longitude } = position.coords;
        try {
          const response = await apiFetch('/api/location', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ latitude, longitude })
          });
          if (!response.ok) throw new Error(await readError(response, '定位解析失败'));
          const data = await response.json();
          if (!data.location) {
            setHint('没能从坐标解析出城市。', true);
            resolve(false);
            return;
          }
          setLocation({ latitude, longitude }, data.location);
          setHint('');
          resolve(true);
        } catch (error) {
          setHint(`定位解析失败：${error.message}`, true);
          resolve(false);
        }
      },
      (error) => {
        setHint(error && error.code === 1
          ? '你拒绝了定位授权。'
          : '无法获取定位。', true);
        resolve(false);
      },
      { timeout: 8000, maximumAge: 600000 }
    );
  });
}

export function chooseCityByHand(reason) {
  const entered = window.prompt(`${reason}\n\n留空则清除已设置的位置。`, state.locationLabel || '');
  if (entered === null) return; // 用户取消，保持原状
  const city = entered.trim();
  if (!city) { setLocation(null, ''); setHint('已清除位置。'); return; }
  setLocation({ city }, city);
  setHint(`位置已设为 ${city}。`);
}

export async function restoreLocation() {
  const saved = loadStoredLocation();
  if (saved) { setLocation(saved.location, saved.label); return; }
  if (!navigator.geolocation || !navigator.permissions?.query) return;
  try {
    const status = await navigator.permissions.query({ name: 'geolocation' });
    if (status.state === 'granted') await locateByCoordinates();
  } catch (error) {
    // 部分浏览器不支持查询 geolocation 权限（含 file:// 与本机 http），静默跳过
  }
}

locateButton.onclick = async () => {
  if (!navigator.geolocation) { chooseCityByHand('当前浏览器不支持自动定位。'); return; }
  setHint('正在请求定位授权…');
  if (!await locateByCoordinates()) {
    chooseCityByHand('自动定位没有成功，请手动输入你所在的城市。');
  }
};
