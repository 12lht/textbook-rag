// 渲染：Markdown、行内公式、引用角标与图注
//
// 由 frontend/app.js 拆出。跨模块可变的共享状态在 core.js 的 store 上，别在本模块另存一份。


export function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
  })[character]);
}

export function looksLikeMath(body) {
  if (!body || body.length > 300) return false;
  if (/\\[a-zA-Z]{2,}/.test(body)) return true;              // \frac \sqrt \pi
  if (/\^|_/.test(body)) return true;                        // x^2、a_{n}
  if (/[{}]/.test(body) && /[=<>+\-]/.test(body)) return true;
  // 教材里最常见的「单字母变量」：$x$、$n$、$f(x)$、$x+y$。
  // 必须以字母开头，`$5`、`$5.00` 这类金额（数字开头）绝不会命中。
  if (/^[A-Za-z]$/.test(body)) return true;                    // x
  if (/^[A-Za-z][A-Za-z0-9]{0,3}$/.test(body)) return true;     // ab、x1
  if (/^[A-Za-z]\s*\([^()]{0,40}\)$/.test(body)) return true;    // f(x)、P(A)
  if (/^[A-Za-z](\s*[-+*/=<>]\s*[A-Za-z0-9]+)+$/.test(body)) return true;  // x+y、n-1
  return false;
}

export const MATH_MASK =
  /(```[\s\S]*?```)|(`[^`\n]*`)|(\$\$[\s\S]+?\$\$)|(\\\[[\s\S]+?\\\])|(\\\([\s\S]+?\\\))|(\$(?![\s$])((?:[^$\\\n]|\\.){1,300}?)(?<![\s\\])\$)/g;

export function maskMath(text) {
  const stash = [];
  const masked = String(text ?? '').replace(
    MATH_MASK,
    (match, fence, code, block, bracket, paren, inline, body) => {
      if (fence || code) return match;
      if (inline !== undefined) {
        if (!looksLikeMath(body)) return match;
        stash.push(`\\(${body}\\)`);
        return `@@MATH${stash.length - 1}@@`;
      }
      stash.push(match);
      return `@@MATH${stash.length - 1}@@`;
    },
  );
  return { masked, stash };
}

export function renderMarkdown(content) {
  const text = String(content ?? '');
  if (!window.marked || !window.DOMPurify) {
    return escapeHtml(text).replace(/\n/g, '<br>');
  }
  // 数学段先抽离成占位符：marked 会把 \( 和 \[ 当转义吃掉反斜杠，
  // KaTeX 就找不到定界符了。占位符是纯文本，能原样穿过 sanitize，
  // 渲染完再把原文（HTML 转义后）放回，交给 renderMathInElement 处理。
  const { masked, stash } = maskMath(text);
  const html = DOMPurify.sanitize(marked.parse(masked, { breaks: true, gfm: true }));
  return html.replace(/@@MATH(\d+)@@/g, (_, index) => escapeHtml(stash[Number(index)] ?? ''));
}

export function decorateCiteCodes(text) {
  return String(text ?? '').replace(/\[(c\d+)\](?!\()/g, '<sup class="cite" data-code="$1">$1</sup>');
}

export function inlineAssets(text, urlMap) {
  if (!urlMap) return String(text ?? '');
  return String(text ?? '').replace(/(!\[[^\]]*\]\()(as_[0-9a-f]{4,})\.png(\))/g,
    (whole, pre, assetId, post) => (urlMap[assetId] ? pre + urlMap[assetId] + post : whole));
}

export function renderAnswer(content, urlMap) {
  return renderMarkdown(inlineAssets(decorateCiteCodes(content), urlMap));
}

export function plainLabel(value) {
  return String(value ?? '')
    .replace(/<sup>(.*?)<\/sup>/gi, '^$1')
    .replace(/<sub>(.*?)<\/sub>/gi, '_$1')
    .replace(/<[^>]{1,20}>/g, '')
    .replace(/\$/g, '')
    .trim();
}

export function sourceMetaText(item) {
  const parts = [];
  const title = plainLabel(item.doc_title);
  const chapter = plainLabel(item.chapter);
  if (title) parts.push(title);
  if (chapter) parts.push(chapter);
  if (item.page_start) {
    parts.push(item.page_end && item.page_end !== item.page_start
      ? `第 ${item.page_start}-${item.page_end} 页`
      : `第 ${item.page_start} 页`);
  }
  const kindText = ({ figure: '图', formula: '公式', table: '表' })[item.kind] || item.kind || '';
  if (kindText) parts.push(kindText);
  return parts.join(' · ') || '教材原文';
}

export function buildAssetMap(items) {
  const map = {};
  (items || []).forEach((item) => {
    if (item.asset_id && item.asset_url) map[item.asset_id] = item.asset_url;
  });
  return map;
}

export function buildCiteIndex(contentNode, items) {
  if (!contentNode || !items || !items.length) return;
  const index = {};
  items.forEach((item) => {
    if (item.code) index[item.code] = item;
  });
  contentNode._citations = index;
}

export function beautifyMath(el) {
  if (!window.renderMathInElement) return;
  renderMathInElement(el, {
    delimiters: [
      { left: '$$', right: '$$', display: true },
      { left: '\\[', right: '\\]', display: true },
      { left: '\\(', right: '\\)', display: false },
    ],
    ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
    throwOnError: false,
  });
}
