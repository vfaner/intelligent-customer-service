#!/usr/bin/env node
/**
 * 管理后台前端自检：语言切换（构建 → 应用 → 切换 → 切回）+ 右上角账号菜单。
 *
 * 为什么需要这个：i18n-check.mjs 用正则从 HTML 里抠 msgid，只能证明「目录覆盖
 * 齐全」；它证明不了浏览器里 innerHTML 抠出来的 msgid 和目录的键真的对得上
 * （空白、实体、属性顺序都可能差一点），也证明不了连续切两次语言还能切回来。
 * 这个脚本用 jsdom 加载真正的 index.html，走真正的 apply() 路径。
 *
 * 用法：node selftest.mjs
 *
 * jsdom 从 ../widget/node_modules 借用（widget 已经把它列为 devDependency），
 * 免得为一个纯静态目录再装一份 node_modules。先跑一次 `cd ../widget && npm i`。
 */
import { createRequire } from 'module';
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(join(here, '..', 'widget', 'package.json'));
let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (_) {
  console.error('缺少 jsdom：请先 cd ../widget && npm install');
  process.exit(1);
}

const html = readFileSync(join(here, 'index.html'), 'utf8');

/**
 * Fresh console in the given language.
 *
 * `runScripts: 'outside-only'` keeps the page's own <script> from running — it
 * would immediately fetch /api/config and fail. locale.js and i18n.js are
 * eval'd by hand instead, which is exactly what the <script src> tags do.
 *
 * The language is seeded through localStorage rather than by faking a timezone:
 * region detection has its own case table in ../widget/selftest.mjs, the boot
 * URL carries no ?lang=, and `stored` outranks every automatic signal, so
 * this stays deterministic.
 */
function boot(stored) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost:8000/admin/' });
  const { window } = dom;
  if (stored) window.localStorage.setItem('cs_lang', stored);
  window.eval(readFileSync(join(here, '..', 'shared', 'locale.js'), 'utf8'));
  window.eval(readFileSync(join(here, 'i18n.js'), 'utf8'));
  const I18N = window.CSAdminI18n;
  I18N.mount();                      // DOMContentLoaded never fires here
  return { window, doc: window.document, I18N };
}

/** All translatable nodes, keyed by the msgid the engine snapshotted. */
function byMsgid(doc) {
  const map = new Map();
  doc.querySelectorAll('[data-i18n]').forEach((el) => {
    if (el._csMsgid !== undefined) map.set(el._csMsgid, el);
  });
  return map;
}

/* 从 index.html 的内联脚本里抠出「账号菜单」那一段。整段脚本不能整体执行
   （一跑就 fetch 后端），但这一段是连续的，用首尾两行做锚点切出来即可。
   锚点被改动时这里会直接抛错，而不是悄悄测了个空壳。 */
const MENU_START = '/** Reflect the signed-in account in the top-bar avatar menu. */';
const MENU_END = "$('miPassword').addEventListener('click', () => showAccount('password'));";
const MENU_SRC = (() => {
  const a = html.indexOf(MENU_START);
  const b = html.indexOf(MENU_END);
  if (a < 0 || b < 0) throw new Error('index.html 里找不到账号菜单那段代码，锚点是不是改了？');
  return html.slice(a, b + MENU_END.length);
})();

/**
 * Boot the console and wire up just the avatar menu.
 *
 * `showPage` / `scrollIntoView` are recorded instead of performed, so a check can
 * assert *where* a menu item takes you. Everything else — the markup, the event
 * listeners, the open/close logic — is the real thing.
 */
function userMenu(stored, user) {
  const ctx = boot(stored);
  const { window, doc } = ctx;
  ctx.shown = [];
  ctx.scrolled = [];
  window.$ = (id) => doc.getElementById(id);
  window.AUTH = { user: () => user || { username: 'admin', display_name: '管理员' } };
  window.showPage = (name) => ctx.shown.push(name);
  // jsdom has no layout, so scrollIntoView simply does not exist on Element.
  window.Element.prototype.scrollIntoView = function () { ctx.scrolled.push(this.id); };
  window.eval(MENU_SRC);
  window.renderUserMenu(user || undefined);
  return ctx;
}

const checks = [
  ['默认语言是简体中文（源语言，无目录）', () => {
    const { I18N, doc } = boot(null);
    // No stored choice and jsdom's timezone/navigator are whatever the host has,
    // so only assert the invariant: zh-CN leaves the markup untouched.
    I18N.setLang('zh-CN');
    return I18N.getLang() === 'zh-CN' &&
      doc.querySelector('[data-page="dashboard"] [data-i18n]').textContent.includes('概览');
  }],

  ['下拉里有四种语言，各用自己的文字', () => {
    const { doc } = boot(null);
    const items = [...doc.querySelectorAll('#langMenu button')];
    const codes = items.map((b) => b.getAttribute('data-lang')).join(',');
    const labels = items.map((b) => b.textContent).join(',');
    return codes === 'zh-CN,zh-TW,ja,en' && labels === '简体中文,繁體中文,日本語,English';
  }],

  ['下拉项带 lang 属性（让浏览器选对字体）', () => {
    const { doc } = boot(null);
    return [...doc.querySelectorAll('#langMenu button')]
      .every((b) => b.getAttribute('lang') === b.getAttribute('data-lang'));
  }],

  ['启动时就是存储里的语言（离线，无需网络）', () => {
    const { I18N, doc } = boot('ja');
    return I18N.getLang() === 'ja' &&
      doc.querySelector('[data-page="dashboard"] [data-i18n]').textContent === '概要';
  }],

  ['切到英语后侧边栏变英语', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    return doc.querySelector('[data-page="dashboard"] [data-i18n]').textContent === 'Dashboard' &&
      doc.querySelector('[data-page="llm"] [data-i18n]').textContent === 'Model Config';
  }],

  ['连续切两次仍然正确（msgid 快照生效）', () => {
    const { I18N, doc } = boot(null);
    const el = () => doc.querySelector('[data-page="llm"] [data-i18n]').textContent;
    I18N.setLang('en');
    if (el() !== 'Model Config') return 'en 阶段就错了：' + el();
    I18N.setLang('ja');       // 若用当前文本做键，这一步会查不到而卡在英文
    if (el() !== 'モデル設定') return 'ja 阶段错了：' + el();
    I18N.setLang('zh-TW');
    return el() === '模型設定' || 'zh-TW 阶段错了：' + el();
  }],

  ['切回简体中文能完全还原', () => {
    const { I18N, doc } = boot(null);
    const before = doc.body.textContent;
    I18N.setLang('ja');
    I18N.setLang('en');
    I18N.setLang('zh-CN');
    return doc.body.textContent === before;
  }],

  ['title 属性也被翻译', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    return doc.getElementById('btnCheckUpdate').getAttribute('title') ===
      'Check for a new version';
  }],

  ['<title> 跟着变', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('ja');
    return doc.title === 'AI カスタマーサポート · 管理コンソール';
  }],

  ['data-i18n-html 的内联标签在 DOM 里活着', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    const bad = [...doc.querySelectorAll('[data-i18n-html]')].filter((el) => {
      const want = (el._csMsgidHtml.match(/<(code|strong|a|em|b)\b/g) || []).length;
      return el.querySelectorAll('code,strong,a,em,b').length !== want;
    });
    return bad.length === 0 || bad.map((e) => e._csMsgidHtml.slice(0, 40));
  }],

  ['每个 msgid 在英语下都真的换了词', () => {
    // The strongest check here: proves the msgid the *browser* computes matches
    // the catalog key, whitespace and entities included.
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    const stale = [...byMsgid(doc).entries()]
      .filter(([msgid, el]) => /[一-鿿]/.test(msgid) && el.textContent === msgid)
      .map(([msgid]) => msgid);
    return stale.length === 0 || stale;
  }],

  ['按钮上的语言名跟着变', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('zh-TW');
    return doc.getElementById('langLabel').textContent === '繁體中文';
  }],

  ['语言名不是待翻译文本（不能带 data-i18n）', () => {
    // If it were marked, apply() would replace 简体中文 with *a translation of
    // it* and the button would say "Simplified Chinese" while showing English.
    // mark-i18n.py excludes it; this is the assertion that keeps it excluded.
    const { doc } = boot(null);
    return doc.getElementById('langLabel').hasAttribute('data-i18n') === false;
  }],

  ['<html lang> 同步（读屏与 CJK 字体要用）', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('ja');
    return doc.documentElement.getAttribute('lang') === 'ja';
  }],

  ['切换被持久化，下次进来还是它', () => {
    const { I18N, window } = boot(null);
    I18N.setLang('en');
    if (window.localStorage.getItem('cs_lang') !== 'en') return '没写进 localStorage';
    return boot('en').I18N.getLang() === 'en';
  }],

  ['当前语言在下拉里高亮', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('ja');
    const on = [...doc.querySelectorAll('#langMenu button')]
      .filter((b) => b.classList.contains('active'));
    return on.length === 1 && on[0].getAttribute('data-lang') === 'ja';
  }],

  ['聊天预览 iframe 带上 ?lang= 重载', () => {
    const { I18N, doc } = boot(null);
    const frame = doc.getElementById('chatFrame');
    frame.src = 'embed.html?api=http://localhost:8000';
    I18N.setLang('ja');
    return frame.src.includes('lang=ja') && frame.src.includes('api=');
  }],

  ['切换会派发 cs-lang-change 事件', () => {
    const { I18N, doc } = boot(null);
    let got = null;
    doc.addEventListener('cs-lang-change', (e) => { got = e.detail.lang; });
    I18N.setLang('en');
    return got === 'en';
  }],

  ['不认识的语言码被忽略，不会把页面弄空', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    I18N.setLang('de');
    return I18N.getLang() === 'en' &&
      doc.querySelector('[data-page="dashboard"] [data-i18n]').textContent === 'Dashboard';
  }],

  ['菜单点击真的能切换（走的是真事件）', () => {
    const { I18N, doc, window } = boot(null);
    const jaBtn = doc.querySelector('#langMenu button[data-lang="ja"]');
    jaBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    return I18N.getLang() === 'ja' &&
      doc.querySelector('[data-page="dashboard"] [data-i18n]').textContent === '概要';
  }],

  ['地球按钮开关菜单并同步 aria-expanded', () => {
    const { doc, window } = boot(null);
    const btn = doc.getElementById('langBtn');
    const menu = doc.getElementById('langMenu');
    const click = () => btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    click();
    if (!menu.classList.contains('open') || btn.getAttribute('aria-expanded') !== 'true') {
      return '没打开';
    }
    click();
    return !menu.classList.contains('open') && btn.getAttribute('aria-expanded') === 'false';
  }],

  ['点页面别处会收起菜单', () => {
    const { doc, window } = boot(null);
    const btn = doc.getElementById('langBtn');
    const menu = doc.getElementById('langMenu');
    btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    doc.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    return !menu.classList.contains('open');
  }],

  ['locale.js 缺失时退回中文而不是崩', () => {
    const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://localhost:8000/admin/' });
    dom.window.eval(readFileSync(join(here, 'i18n.js'), 'utf8'));   // 只加载 i18n.js
    const I18N = dom.window.CSAdminI18n;
    I18N.mount();
    return I18N.getLang() === 'zh-CN' && I18N.LOCALES.length === 4;
  }],

  ['mount() 调两次不会出现重复下拉项', () => {
    const { doc, I18N } = boot(null);
    I18N.mount();
    return doc.querySelectorAll('#langMenu button').length === 4;
  }],

  /* ── 右上角账号头像下拉 ─────────────────────────────────
     和语言下拉一样是纯 DOM 行为，但它的代码在 index.html 的内联
     <script> 里，而那段脚本一跑就会去 fetch 后端。所以这里把「账号
     菜单」那一段单独抠出来 eval，其余依赖（$ / AUTH / showPage）
     用桩替掉 —— 测的是真正会上线的那几行，不是复刻品。 */

  ['账号头像在顶栏最右侧', () => {
    const { doc } = boot(null);
    const actions = doc.querySelector('.topbar-actions');
    return actions.lastElementChild.id === 'userMenu';
  }],

  ['未渲染前头像是隐藏的（鉴权关闭时没有账号可显示）', () => {
    const { doc } = boot(null);
    return doc.getElementById('userMenu').hidden === true;
  }],

  ['渲染后显示昵称首字母，emoji 昵称不会被切成半个字符', () => {
    const a = userMenu(null, { username: 'admin', display_name: '管理员' });
    if (a.doc.getElementById('userMenu').hidden !== false) return '仍然隐藏';
    if (a.doc.getElementById('userInitial').textContent !== '管') return '首字母错';
    if (a.doc.getElementById('userName').textContent !== '管理员') return '昵称错';
    const b = userMenu(null, { username: 'admin', display_name: '🐱喵' });
    return b.doc.getElementById('userInitial').textContent === '🐱';
  }],

  ['没有昵称时副标题不重复用户名', () => {
    const a = userMenu(null, { username: 'admin' });
    if (a.doc.getElementById('userSub').textContent !== '') return '重复了：' + a.doc.getElementById('userSub').textContent;
    const b = userMenu(null, { username: 'admin', display_name: '管理员' });
    return b.doc.getElementById('userSub').textContent === 'admin';
  }],

  ['鼠标扫过头像就展开', () => {
    const { doc, window } = userMenu();
    doc.getElementById('userMenu').dispatchEvent(new window.MouseEvent('mouseenter'));
    return doc.getElementById('userMenu').classList.contains('open');
  }],

  ['移开鼠标不立即收起（要跨过头像与面板之间的空隙）', () => {
    const { doc, window } = userMenu();
    const menu = doc.getElementById('userMenu');
    menu.dispatchEvent(new window.MouseEvent('mouseenter'));
    menu.dispatchEvent(new window.MouseEvent('mouseleave'));
    return menu.classList.contains('open');
  }],

  ['点击头像开合，aria-expanded 跟着变', () => {
    const { doc, window } = userMenu();
    const menu = doc.getElementById('userMenu');
    const btn = doc.getElementById('userAvatarBtn');
    const click = () => btn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    click();
    if (!menu.classList.contains('open')) return '第一次点击没展开';
    if (btn.getAttribute('aria-expanded') !== 'true') return 'aria-expanded 没置 true';
    click();
    return !menu.classList.contains('open') && btn.getAttribute('aria-expanded') === 'false';
  }],

  ['点别处、按 Esc 都会收起', () => {
    const { doc, window } = userMenu();
    const menu = doc.getElementById('userMenu');
    // 每次都先确认真的展开了，否则「收起」是空断言
    const open = () => {
      menu.dispatchEvent(new window.MouseEvent('mouseenter'));
      return menu.classList.contains('open');
    };
    if (!open()) return '压根没展开';
    doc.body.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    if (menu.classList.contains('open')) return '点别处没收起';
    if (!open()) return '第二次没展开';
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape' }));
    return !menu.classList.contains('open');
  }],

  ['个人信息 / 修改密码 分别跳到账号页的两张卡片', () => {
    const { doc, window, shown, scrolled } = userMenu();
    doc.getElementById('miProfile').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    if (shown.join() !== 'account') return 'showPage 没被调用：' + shown.join();
    if (scrolled.join() !== 'cardProfile') return '滚到了：' + scrolled.join();
    doc.getElementById('miPassword').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    if (scrolled.join() !== 'cardProfile,cardPassword') return '滚到了：' + scrolled.join();
    // 改密码时光标直接落在「原密码」上，少一次点击
    return doc.activeElement === doc.getElementById('acctOldPw');
  }],

  ['菜单三项都会跟着切换语言', () => {
    const { I18N, doc } = boot(null);
    I18N.setLang('en');
    const text = (id) => doc.getElementById(id).textContent.trim();
    return text('miProfile').endsWith('Profile') &&
      text('miPassword').endsWith('Change password') &&
      text('miLogout').endsWith('Sign out');
  }],
];

/* ═══ Release Notes 的 Markdown 渲染 ═══════════════════════════
   「检测更新」弹窗把 GitHub Release 的正文渲染成 HTML。正文是远端内容，
   写 Release 的人能往里塞任意 HTML，所以 renderMarkdown() 的实现约定是
   **先整体转义、再在转义后的文本上做替换** —— 页面上真正生效的标签只能
   由它自己生成。下面的用例主要就是钉住这条约定：哪天有人为了「支持一下
   原生 HTML」把顺序调过来，这里必须红。

   renderMarkdown 定义在 index.html 的内联 <script> 里，而本脚本以
   runScripts:'outside-only' 加载页面（页面脚本一跑就会 fetch /api/config
   然后失败），所以把那段源码切出来单独求值 —— 测的仍是线上那一份。 */
const MD_FROM = 'function escapeHtml(s)';
const MD_TO = '/* ----------- Boot ----------- */';
const mdA = html.indexOf(MD_FROM), mdB = html.indexOf(MD_TO);
if (mdA < 0 || mdB < mdA) {
  console.error('切不出 renderMarkdown —— index.html 里的锚点注释被改了？');
  process.exit(1);
}
const renderMarkdown = new Function(html.slice(mdA, mdB) + '\nreturn renderMarkdown;')();

/* reindexNeeds 同理：决定「换模型后是否提示重建」的纯函数在内联脚本里，
   整页脚本一跑就 fetch，所以用注释锚点切出这一段单独求值。函数体或规则
   被改动而忘了同步自测时，下面的用例会直接红。 */
const RN_START = '/* reindexNeeds-extract-start */';
const RN_END = '/* reindexNeeds-extract-end */';
const rnA = html.indexOf(RN_START), rnB = html.indexOf(RN_END);
if (rnA < 0 || rnB < rnA) {
  console.error('切不出 reindexNeeds —— index.html 里的锚点注释被改了？');
  process.exit(1);
}
const reindexNeeds = new Function(
  html.slice(rnA + RN_START.length, rnB) + ';return reindexNeeds;')();
const SIG = { provider: 'p', model: 'm', base_url: 'https://e/v1', dim: 1024 };

checks.push(
  ['reindexNeeds：0 个已入库文档不需要重建（首次入库会补签名）', () => {
    return reindexNeeds(SIG, null, 0) === false &&
           reindexNeeds(SIG, { ...SIG, model: 'old' }, 0) === false;
  }],
  ['reindexNeeds：0 文档但集合锁在别的维度 → 必须重建', () => {
    // 文档全删光 / 全部失败时，磁盘集合仍然锁维，这是必须报的情形。
    return reindexNeeds(SIG, null, 0, 2048) === true &&
           reindexNeeds({ ...SIG, dim: 1024 }, { ...SIG }, 0, 2048) === true;
  }],
  ['reindexNeeds：集合维度与当前一致时 0 文档不提示', () => {
    return reindexNeeds(SIG, null, 0, 1024) === false &&
           reindexNeeds(SIG, null, 0, null) === false;
  }],
  ['reindexNeeds：无签名的旧库 + 有已入库文档 → 建议重建', () => {
    return reindexNeeds(SIG, null, 3) === true;
  }],
  ['reindexNeeds：签名一致 → 不重建', () => {
    return reindexNeeds(SIG, { ...SIG }, 9) === false;
  }],
  ['reindexNeeds：model/base_url/dim/provider 任一变化 → 重建', () => {
    return reindexNeeds(SIG, { ...SIG, model: 'm2' }, 1) === true &&
           reindexNeeds(SIG, { ...SIG, base_url: 'https://other/v1' }, 1) === true &&
           reindexNeeds(SIG, { ...SIG, dim: 2048 }, 1) === true &&
           reindexNeeds(SIG, { ...SIG, provider: 'q' }, 1) === true;
  }],
  ['reindexNeeds：同维度不同模型也必须重建（向量空间不同）', () => {
    return reindexNeeds({ ...SIG, dim: 1024 }, { ...SIG, model: 'other', dim: 1024 }, 2) === true;
  }],
  ['reindexNeeds：缺少当前签名时不谎报（宁可不提示也不误清空）', () => {
    return reindexNeeds(null, SIG, 5) === false;
  }],
);

const md = (name, src, fn) => checks.push(['md: ' + name, () => {
  const out = renderMarkdown(src);
  return fn(out) === true ? true : '得到 ' + JSON.stringify(out);
}]);
const lacks = (...ss) => o => ss.every(s => !o.includes(s));
const holds = (...ss) => o => ss.every(s => o.includes(s));

// —— 安全：远端正文里的 HTML 必须只以字面量出现 ——
md('script 标签不生效', '<script>alert(1)</script>', lacks('<script'));
md('img onerror 不生效', '<img src=x onerror=alert(1)>', lacks('<img'));
md('javascript: 链接不生成 <a>', '[点我](javascript:alert(1))', lacks('<a '));
md('data: 链接不生成 <a>', '[x](data:text/html,y)', lacks('<a '));
md('链接文本挡不住属性闭合', '[x](https://a.cn/"onmouseover="alert(1))',
  lacks('onmouseover="alert'));
md('http 链接照常放行', '[文档](https://a.cn/b?x=1&y=2)',
  holds('<a href="https://a.cn/b?x=1&amp;y=2"', 'rel="noopener noreferrer"'));
// 行内代码用 <cN> 占位；正文已转义过，伪造不出 < ，这条钉住它
md('占位符无法从正文伪造', '正文写 <c0> 再来个 `真代码`',
  o => holds('&lt;c0&gt;', '<code>真代码</code>')(o) && lacks('undefined')(o));

// —— 块级 ——
md('## 降到 h4（别抢弹窗标题层级）', '## 新功能', holds('<h4>新功能</h4>'));
md('列表 + 缩进续行合并', '- 第一行\n  第二行\n- 下一项',
  holds('<li>第一行 第二行</li>', '<li>下一项</li>'));
md('表格', '| A | B |\n|---|---|\n| 1 | 2 |',
  holds('<th>A</th>', '<td>1</td>', '<td>2</td>'));
// 引用要匹配 &gt; 而不是 > —— 转义发生在块解析之前，写成 > 会永远不匹配
md('引用（转义后仍能识别）', '> 注意事项', holds('<blockquote>注意事项</blockquote>'));
md('代码块内不再解析 markdown', '```\n- 不是列表 **不加粗**\n```',
  lacks('<li>', '<strong>'));
// 后端把正文截断到 1500 字（admin.py），围栏可能没有收尾
md('未闭合代码块（正文被截断）', '```bash\ngit pull\ncd backend',
  holds('<pre><code>git pull\ncd backend</code></pre>'));

// —— 不该误伤的普通文本 ——
md('中文里的孤立数字不当占位符', '共 3 个文件，另有 0 处告警',
  o => holds('共 3 个文件')(o) && lacks('<code>', 'undefined')(o));
md('大写下划线变量名不斜体', 'APP_GITHUB_REPO 和 APP_VERSION', lacks('<em>'));
md('空正文不报错', '', o => o === '');

let pass = 0;
const fails = [];
for (const [name, fn] of checks) {
  let r;
  try { r = fn(); } catch (e) { r = 'THREW: ' + e.message; }
  if (r === true) { pass++; console.log('  ✓ ' + name); }
  else { fails.push([name, r]); console.log('  ✗ ' + name + '  → ' + JSON.stringify(r)); }
}

console.log(`\n${pass}/${checks.length} 通过`);
if (fails.length) {
  console.log('\n失败详情：');
  fails.forEach(([n, r]) => console.log('  ' + n + ': ' + JSON.stringify(r, null, 1)));
  process.exit(1);
}
console.log('✅ 管理后台语言切换 + 账号菜单正常');
