/*!
 * CustomerService — embeddable chat widget (no build step required).
 * Usage:
 *   <script src="/path/customer-service.js"></script>
 *   <script>CustomerService.init({ apiUrl: 'https://api.example.com' });</script>
 *
 * Public API on window.CustomerService:
 *   init({ apiUrl, configId, title, subtitle, accent, position, autoOpen, sessionId, lang, onReady })
 *   open() / close() / toggle()
 *   sendMessage(text)
 *   setLang(code) / getLang() / resolveLang(input)
 *   destroy()
 *
 * Languages: zh-CN · zh-TW · ja · en. The default is picked from the visitor's
 * region with no network access at all — see LOCALE CORE below.
 */
(function () {
  'use strict';

  // -------------------------------------------------------------------------
  // LOCALE CORE — offline region/language detection
  //
  // ⚠ This is an inlined copy of frontend/shared/locale.js, which is the
  // authoritative implementation. The widget is distributed as a single file
  // (package.json ships only customer-service.js), so it cannot import it.
  // selftest.mjs evals both and asserts resolve() agrees across a case table —
  // if you change one and not the other, the test fails. Keep them in sync.
  //
  // Why no IP geolocation: the target is intranet/offline deployment. A 10.x
  // address carries no region and no online geo API is reachable. So we use
  // system timezone (→ region) with browser language as the fallback.
  // -------------------------------------------------------------------------
  var LANG_STORAGE_KEY = 'cs_lang';
  var DEFAULT_LANG = 'en';

  var LOCALE_LIST = [
    { code: 'zh-CN', label: '简体中文', htmlLang: 'zh-CN' },
    { code: 'zh-TW', label: '繁體中文', htmlLang: 'zh-TW' },
    { code: 'ja',    label: '日本語',   htmlLang: 'ja'    },
    { code: 'en',    label: 'English',  htmlLang: 'en'    }
  ];
  var SUPPORTED_LANGS = LOCALE_LIST.map(function (l) { return l.code; });

  // Canonical IANA names + legacy aliases (modern engines canonicalise
  // PRC → Asia/Shanghai etc., but old ones may return the alias verbatim).
  var TZ_TO_LOCALE = {
    'Asia/Shanghai': 'zh-CN', 'Asia/Chongqing': 'zh-CN', 'Asia/Chungking': 'zh-CN',
    'Asia/Harbin': 'zh-CN', 'Asia/Urumqi': 'zh-CN', 'Asia/Kashgar': 'zh-CN', 'PRC': 'zh-CN',
    'Asia/Taipei': 'zh-TW', 'ROC': 'zh-TW',
    'Asia/Hong_Kong': 'zh-TW', 'Hongkong': 'zh-TW',
    'Asia/Macau': 'zh-TW', 'Asia/Macao': 'zh-TW',
    'Asia/Tokyo': 'ja', 'Japan': 'ja'
  };
  var TRADITIONAL_SUBTAGS = { hant: 1, cht: 1, tw: 1, hk: 1, mo: 1 };
  var SIMPLIFIED_SUBTAGS = { hans: 1, chs: 1, cn: 1, sg: 1, my: 1 };

  function isSupportedLang(code) {
    return SUPPORTED_LANGS.indexOf(code) !== -1;
  }

  function langFromTag(tag) {
    if (!tag) return null;
    var parts = String(tag).toLowerCase().replace(/_/g, '-').split('-');
    var primary = parts[0];
    if (primary === 'ja') return 'ja';
    if (primary === 'en') return 'en';
    if (primary === 'zh' || primary === 'yue') {
      var i;
      for (i = 1; i < parts.length; i++) if (TRADITIONAL_SUBTAGS[parts[i]]) return 'zh-TW';
      for (i = 1; i < parts.length; i++) if (SIMPLIFIED_SUBTAGS[parts[i]]) return 'zh-CN';
      return primary === 'yue' ? 'zh-TW' : 'zh-CN';
    }
    // null (not 'en') so an ordered preference list keeps scanning: for
    // ['fr-FR','zh-CN'] the answer should be zh-CN, not English.
    return null;
  }

  function normalizeLang(code) {
    if (!code) return null;
    var raw = String(code).trim();
    if (isSupportedLang(raw)) return raw;
    var guess = langFromTag(raw);
    return guess && isSupportedLang(guess) ? guess : null;
  }

  function langFromLanguages(list) {
    if (!list) return null;
    var arr = typeof list === 'string' ? [list] : list;
    for (var i = 0; i < arr.length; i++) {
      var hit = langFromTag(arr[i]);
      if (hit) return hit;
    }
    return null;
  }

  /**
   * Pure resolver. Priority: manual choice > explicit option > timezone
   * (region) > browser language > English.
   *
   * Timezone wins over language because the requirement is "pick by region":
   * a machine in Taipei running an English OS is still Asia/Taipei and should
   * get Traditional Chinese, even though navigator.language says en.
   */
  function resolveLang(input) {
    var o = input || {};
    var stored = normalizeLang(o.stored);
    if (stored) return { locale: stored, source: 'stored' };
    var explicit = normalizeLang(o.explicit);
    if (explicit) return { locale: explicit, source: 'explicit' };
    var byTz = o.timeZone ? (TZ_TO_LOCALE[o.timeZone] || null) : null;
    if (byTz) return { locale: byTz, source: 'timezone' };
    var byLang = langFromLanguages(o.languages);
    if (byLang) return { locale: byLang, source: 'language' };
    return { locale: DEFAULT_LANG, source: 'default' };
  }

  function currentTimeZone() {
    // Missing on very old engines → undefined → falls through to language.
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || null; }
    catch (_) { return null; }
  }

  function currentLanguages() {
    var nav = window.navigator || {};
    if (nav.languages && nav.languages.length) return nav.languages;
    var one = nav.language || nav.userLanguage;
    return one ? [one] : [];
  }

  function readStoredLang() {
    try { return window.localStorage ? window.localStorage.getItem(LANG_STORAGE_KEY) : null; }
    catch (_) { return null; }   // private mode / blocked storage
  }

  function saveLang(code) {
    try { if (window.localStorage) window.localStorage.setItem(LANG_STORAGE_KEY, code); }
    catch (_) { /* unstorable is fine — reverts to auto-detect next load */ }
  }

  function detectLang(explicit) {
    return resolveLang({
      stored: readStoredLang(),
      explicit: explicit,
      timeZone: currentTimeZone(),
      languages: currentLanguages()
    }).locale;
  }

  // -------------------------------------------------------------------------
  // STRINGS
  // -------------------------------------------------------------------------
  var STRINGS = {
    'zh-CN': {
      title: '智能客服',
      subtitle: '在线',
      welcome: '您好，请问有什么可以帮您？',
      placeholder: '输入您的问题…',
      uploadHint: '支持 .txt / .md / .docx / .xlsx / .pdf，单文件 ≤ 20MB',
      openAria: '打开客服',
      closeAria: '关闭',
      uploadTitle: '上传文件',
      sendTitle: '发送',
      langTitle: '切换语言',
      sources: '引用 {n} 条资料',
      uploading: '正在上传：{name}（{size} KB）…',
      parsing: '已上传 {name}，正在解析并入库…',
      ingested: '✅ {name} 已入库：{n} 个片段，现在可以就它提问了',
      ingesting: '⏳ {name} 已转入后台入库，完成后即可提问（大文件需要一点时间）',
      ingestFailed: '⚠ {name} 入库失败：{err}',
      uploadFailed: '⚠ 上传失败：{err}',
      unknownError: '未知错误',
      genericError: '出错了',
      streamInterrupted: '（回答被中断，以上内容可能不完整）',
      switched: '已切换为简体中文，接下来我会用简体中文回答。'
    },
    'zh-TW': {
      title: '智能客服',
      subtitle: '線上',
      welcome: '您好，請問有什麼可以為您服務？',
      placeholder: '請輸入您的問題…',
      uploadHint: '支援 .txt / .md / .docx / .xlsx / .pdf，單一檔案 ≤ 20MB',
      openAria: '開啟客服',
      closeAria: '關閉',
      uploadTitle: '上傳檔案',
      sendTitle: '傳送',
      langTitle: '切換語言',
      sources: '引用 {n} 筆資料',
      uploading: '正在上傳：{name}（{size} KB）…',
      parsing: '已上傳 {name}，正在解析並建立索引…',
      ingested: '✅ {name} 已建立索引：{n} 個片段，現在可以針對它提問了',
      ingesting: '⏳ {name} 已轉入後台建立索引，完成後即可提問（大檔案需要一點時間）',
      ingestFailed: '⚠ {name} 建立索引失敗：{err}',
      uploadFailed: '⚠ 上傳失敗：{err}',
      unknownError: '未知錯誤',
      genericError: '發生錯誤',
      streamInterrupted: '（回答被中斷，以上內容可能不完整）',
      switched: '已切換為繁體中文，接下來我會用繁體中文回答。'
    },
    'ja': {
      title: 'AIカスタマーサポート',
      subtitle: 'オンライン',
      welcome: 'こんにちは。ご用件をお伺いします。',
      placeholder: 'ご質問を入力してください…',
      uploadHint: '.txt / .md / .docx / .xlsx / .pdf に対応、1ファイル 20MB まで',
      openAria: 'サポートチャットを開く',
      closeAria: '閉じる',
      uploadTitle: 'ファイルを添付',
      sendTitle: '送信',
      langTitle: '言語を切り替える',
      sources: '参照した資料 {n} 件',
      uploading: 'アップロード中：{name}（{size} KB）…',
      parsing: '{name} をアップロードしました。解析して登録しています…',
      ingested: '✅ {name} を登録しました：{n} 件のチャンク。この内容について質問できます',
      ingesting: '⏳ {name} のバックグラウンド登録を開始しました。完了後に質問できます（大きなファイルは時間がかかります）',
      ingestFailed: '⚠ {name} の登録に失敗しました：{err}',
      uploadFailed: '⚠ アップロードに失敗しました：{err}',
      unknownError: '不明なエラー',
      genericError: 'エラーが発生しました',
      streamInterrupted: '（回答が中断されました。上記の内容は不完全な可能性があります）',
      switched: '日本語に切り替えました。これ以降は日本語でお答えします。'
    },
    'en': {
      title: 'Customer Support',
      subtitle: 'Online',
      welcome: 'Hello! How can I help you today?',
      placeholder: 'Type your question…',
      uploadHint: 'Supports .txt / .md / .docx / .xlsx / .pdf, up to 20MB per file',
      openAria: 'Open support chat',
      closeAria: 'Close',
      uploadTitle: 'Attach a file',
      sendTitle: 'Send',
      langTitle: 'Change language',
      sources: '{n} source(s) cited',
      uploading: 'Uploading {name} ({size} KB)…',
      parsing: 'Uploaded {name}. Parsing and indexing…',
      ingested: '✅ {name} indexed: {n} chunk(s). You can ask about it now.',
      ingesting: '⏳ {name} is being indexed in the background — you can ask about it shortly (large files take a while)',
      ingestFailed: '⚠ Could not index {name}: {err}',
      uploadFailed: '⚠ Upload failed: {err}',
      unknownError: 'Unknown error',
      genericError: 'Something went wrong',
      streamInterrupted: '(the reply was interrupted; the text above may be incomplete)',
      switched: 'Switched to English. I\'ll reply in English from now on.'
    }
  };

  /**
   * The values the backend ships as *defaults* for 客服名称 / 欢迎语.
   *
   * Without this the locale defaults would be dead code: CSConfigIn defaults
   * `name` and `welcome_message` to non-empty Chinese strings, and
   * applyServerConfig() overwrites whenever the server value is truthy — so a
   * Japanese visitor would always get the Chinese welcome even on a fresh
   * install nobody had configured. A server value that still equals a factory
   * default means "the admin never customised this", so the locale default
   * wins. Anything the admin actually typed is respected for every language.
   */
  var FACTORY_VALUES = {
    title: ['智能客服', '智能客服小助手'],
    welcome: ['您好，请问有什么可以帮您？']
  };

  /** Keys in cfg that come from the string catalog rather than the caller. */
  var LOCALIZED_KEYS = ['title', 'subtitle', 'welcome', 'placeholder', 'uploadHint'];

  var lang = DEFAULT_LANG;

  /** Look up a string, interpolating {placeholders}. */
  function t(key, params) {
    var table = STRINGS[lang] || STRINGS[DEFAULT_LANG];
    var s = table[key];
    if (s === undefined) s = (STRINGS[DEFAULT_LANG][key] !== undefined)
      ? STRINGS[DEFAULT_LANG][key] : key;
    if (!params) return s;
    return s.replace(/\{(\w+)\}/g, function (m, k) {
      return params[k] !== undefined ? params[k] : m;
    });
  }

  // -------------------------------------------------------------------------
  // Config + state
  // -------------------------------------------------------------------------
  const DEFAULTS = {
    apiUrl: 'http://localhost:8000',
    configId: '',
    // title / subtitle / welcome / placeholder / uploadHint are filled from
    // STRINGS[lang] by applyLocaleStrings(). Pass them to init() to override.
    title: null,
    avatar: '',                 // image URL; falls back to the title's initial
    subtitle: null,
    welcome: null,
    placeholder: null,
    accent: '#0a66c2',
    position: 'right',          // 'left' | 'right'
    autoOpen: false,
    sessionId: null,
    // Off by default: /api/documents/* now requires an admin token, so a
    // visitor's upload would fail with 401. Turn on only if you have put your
    // own authenticated proxy in front of the upload endpoint.
    enableUpload: false,
    uploadHint: null,
    // 'auto' → detect from region/language (see LOCALE CORE). Or pin one of
    // 'zh-CN' | 'zh-TW' | 'ja' | 'en'.
    lang: 'auto',
    // Show the in-panel language switcher. Turn off if the host page provides
    // its own and drives the widget via CustomerService.setLang().
    showLangSwitcher: true,
    // Pull 客服名称 / 头像 / 欢迎语 / 联系方式 from GET /api/config/public so
    // the admin console is the single source of truth. Anything passed to
    // init() explicitly still wins.
    useServerConfig: true,
    onReady: null,
    // Fired after the language changes (manual switch or initial detection).
    onLangChange: null,
  };

  let cfg = Object.assign({}, DEFAULTS);
  let sessionId = null;
  let isOpen = false;
  let rootEl = null;
  let messagesEl = null;
  let inputEl = null;
  let buttonEl = null;
  let panelEl = null;
  let fileBtnEl = null;
  let fileInputEl = null;
  let abortController = null;
  let langMenuEl = null;
  /** Keys the caller passed to init() — these always beat locale + server. */
  let explicitKeys = new Set();
  /** Values the admin genuinely customised in the console (non-factory). */
  let serverStrings = {};

  /**
   * Resolve the localizable cfg fields for the current language.
   * Precedence: init() option > admin console value > locale default.
   */
  function applyLocaleStrings() {
    LOCALIZED_KEYS.forEach(function (k) {
      if (explicitKeys.has(k)) return;
      cfg[k] = serverStrings[k] !== undefined ? serverStrings[k] : t(k);
    });
  }


  // -------------------------------------------------------------------------
  // Styles (injected once)
  // -------------------------------------------------------------------------
  const CSS = `
  .cs-root { all: initial; position: fixed; z-index: 2147483600; font-family: -apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', 'Segoe UI', Roboto, sans-serif; color: #222; }
  .cs-root * { box-sizing: border-box; }
  .cs-btn { width: 56px; height: 56px; border-radius: 50%; background: var(--cs-accent, #0a66c2); color: #fff; border: 0; cursor: pointer; box-shadow: 0 6px 18px rgba(0,0,0,.18); display: flex; align-items: center; justify-content: center; transition: transform .15s ease, box-shadow .15s ease; }
  .cs-btn:hover { transform: translateY(-2px); box-shadow: 0 10px 24px rgba(0,0,0,.22); }
  .cs-btn svg { width: 26px; height: 26px; }
  .cs-pos-right { right: 20px; bottom: 20px; }
  .cs-pos-left  { left: 20px;  bottom: 20px; }
  .cs-panel { position: absolute; bottom: 76px; width: 360px; max-width: calc(100vw - 32px); height: 540px; max-height: calc(100vh - 120px); background: #fff; border-radius: 12px; box-shadow: 0 16px 48px rgba(0,0,0,.18); display: none; flex-direction: column; overflow: hidden; }
  .cs-pos-right .cs-panel { right: 0; }
  .cs-pos-left  .cs-panel { left: 0; }
  .cs-panel.open { display: flex; animation: cs-pop .18s ease-out; }
  @keyframes cs-pop { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
  .cs-header { display: flex; align-items: center; gap: 10px; padding: 12px 14px; background: var(--cs-accent, #0a66c2); color: #fff; }
  .cs-header .avatar { width: 36px; height: 36px; border-radius: 50%; background: rgba(255,255,255,.2); display: flex; align-items: center; justify-content: center; font-weight: 600; overflow: hidden; flex-shrink: 0; }
  .cs-header .avatar img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .cs-input .cs-upload svg { display: block; }
  .cs-header .meta { flex: 1; min-width: 0; }
  .cs-header .title { font-weight: 600; font-size: 14px; line-height: 1.2; }
  .cs-header .subtitle { font-size: 12px; opacity: .85; }
  .cs-header .close { background: transparent; border: 0; color: #fff; cursor: pointer; font-size: 22px; line-height: 1; padding: 4px; }
  /* Language switcher. The menu drops into the messages area — that stays
     inside .cs-panel, so the panel's overflow:hidden doesn't clip it. */
  .cs-lang { position: relative; flex-shrink: 0; }
  .cs-lang-btn { background: transparent; border: 0; color: #fff; cursor: pointer; padding: 5px; display: flex; align-items: center; border-radius: 6px; }
  .cs-lang-btn:hover { background: rgba(255,255,255,.18); }
  .cs-lang-btn svg { width: 18px; height: 18px; display: block; }
  .cs-lang-menu { position: absolute; top: 100%; right: 0; margin-top: 6px; background: #fff; border-radius: 8px; box-shadow: 0 8px 24px rgba(0,0,0,.20); padding: 4px; min-width: 136px; display: none; z-index: 5; }
  .cs-lang-menu.open { display: block; }
  .cs-lang-menu button { display: block; width: 100%; text-align: left; background: transparent; border: 0; padding: 8px 10px; font-size: 13px; font-family: inherit; color: #222; cursor: pointer; border-radius: 6px; white-space: nowrap; }
  .cs-lang-menu button:hover { background: #f0f2f5; }
  .cs-lang-menu button.active { color: var(--cs-accent, #0a66c2); font-weight: 600; }
  .cs-messages { flex: 1; overflow-y: auto; padding: 12px; background: #f7f8fa; }
  .cs-msg { margin: 6px 0; display: flex; }
  .cs-msg.user { justify-content: flex-end; }
  /* pre-wrap keeps newlines visible while raw text streams in; once the
     Markdown is rendered into real elements it would add phantom blank
     lines, so the md class turns it off. */
  .cs-msg .bubble { max-width: 78%; padding: 8px 12px; border-radius: 12px; line-height: 1.5; font-size: 14px; word-wrap: break-word; white-space: pre-wrap; }
  .cs-msg .bubble.md { white-space: normal; }
  .cs-msg .bubble.md p, .cs-msg .bubble.md li { white-space: pre-wrap; }
  .cs-msg.user .bubble { background: var(--cs-accent, #0a66c2); color: #fff; border-bottom-right-radius: 2px; }
  .cs-msg.assistant .bubble { background: #fff; color: #222; border: 1px solid #eef0f3; border-bottom-left-radius: 2px; }
  .cs-msg .bubble a.cs-link { color: var(--cs-accent, #0a66c2); text-decoration: underline; word-break: break-all; }
  .cs-msg .bubble a.cs-link:hover { text-decoration: none; opacity: .8; }
  .cs-msg.user .bubble a.cs-link { color: #fff; }
  /* Rendered Markdown inside a chat bubble — compact, no huge headings. */
  .cs-msg .bubble p { margin: 0 0 8px; }
  .cs-msg .bubble p:last-child { margin-bottom: 0; }
  .cs-msg .bubble .cs-md-h { font-weight: 600; margin: 10px 0 6px; line-height: 1.35; }
  .cs-msg .bubble .cs-md-h:first-child { margin-top: 0; }
  .cs-msg .bubble .cs-md-h1 { font-size: 16px; }
  .cs-msg .bubble .cs-md-h2 { font-size: 15px; }
  .cs-msg .bubble .cs-md-h3 { font-size: 14px; }
  .cs-msg .bubble .cs-md-h4 { font-size: 13px; color: #4b5563; }
  .cs-msg .bubble ul, .cs-msg .bubble ol { margin: 4px 0 8px; padding-left: 20px; }
  .cs-msg .bubble li { margin: 3px 0; }
  .cs-msg .bubble li:last-child { margin-bottom: 0; }
  .cs-msg .bubble strong { font-weight: 600; }
  .cs-msg .bubble code { background: #f1f3f5; color: #c7254e; padding: 1px 5px; border-radius: 3px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
  .cs-msg .bubble pre { background: #1f2937; color: #e5e7eb; padding: 10px 12px; border-radius: 6px; overflow-x: auto; margin: 6px 0 8px; }
  .cs-msg .bubble pre code { background: none; color: inherit; padding: 0; font-size: 12px; line-height: 1.5; }
  .cs-msg .bubble blockquote { margin: 6px 0; padding: 4px 10px; border-left: 3px solid #d1d5db; color: #4b5563; background: #f9fafb; white-space: pre-wrap; }
  .cs-msg .bubble hr { border: 0; border-top: 1px solid #e5e7eb; margin: 10px 0; }
  .cs-msg.user .bubble code { background: rgba(255,255,255,.22); color: #fff; }
  .cs-msg.system .bubble { background: #fff7e6; color: #663c00; border: 1px solid #ffe7ba; font-size: 12px; }
  .cs-sources { margin-top: 6px; font-size: 12px; color: #666; }
  .cs-sources details { background: #f0f2f5; border-radius: 6px; padding: 4px 8px; }
  .cs-sources summary { cursor: pointer; }
  .cs-sources ol { padding-left: 20px; margin: 4px 0 0; }
  .cs-typing { display: inline-block; }
  .cs-typing span { display: inline-block; width: 4px; height: 4px; margin: 0 1px; background: #999; border-radius: 50%; animation: cs-dot 1.2s infinite ease-in-out; }
  .cs-typing span:nth-child(2) { animation-delay: .15s; }
  .cs-typing span:nth-child(3) { animation-delay: .3s; }
  @keyframes cs-dot { 0%, 80%, 100% { transform: scale(.6); opacity: .4; } 40% { transform: scale(1); opacity: 1; } }
  .cs-input { border-top: 1px solid #eef0f3; padding: 8px; background: #fff; display: flex; gap: 6px; align-items: flex-end; }
  .cs-input textarea { flex: 1; min-height: 36px; max-height: 120px; padding: 8px 10px; border: 1px solid #e0e3e8; border-radius: 8px; resize: none; font: inherit; font-size: 14px; outline: none; }
  .cs-input textarea:focus { border-color: var(--cs-accent, #0a66c2); }
  .cs-input button { border: 0; background: var(--cs-accent, #0a66c2); color: #fff; width: 40px; height: 40px; border-radius: 8px; cursor: pointer; font-size: 18px; }
  .cs-input button:disabled { opacity: .5; cursor: not-allowed; }
  .cs-input .cs-upload { background: #f0f2f5; color: #555; }
  .cs-upload-hint { font-size: 11px; color: #999; padding: 0 12px 8px; background: #fff; }
  .cs-error { color: #c0392b; font-size: 12px; margin: 4px 12px; }
  `;

  function injectStyles() {
    if (document.getElementById('cs-styles')) return;
    const s = document.createElement('style');
    s.id = 'cs-styles';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  // -------------------------------------------------------------------------
  // DOM helpers
  // -------------------------------------------------------------------------
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) for (const k in attrs) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'style') node.setAttribute('style', attrs[k]);
      else if (k === 'html') node.innerHTML = attrs[k];
      else if (k.startsWith('on') && typeof attrs[k] === 'function') node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== undefined && attrs[k] !== null) node.setAttribute(k, attrs[k]);
    }
    if (children) {
      (Array.isArray(children) ? children : [children]).forEach(c => {
        if (c == null) return;
        node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
      });
    }
    return node;
  }

  function escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
  }

  // -------------------------------------------------------------------------
  // API client
  // -------------------------------------------------------------------------
  function baseHeaders() {
    return { 'Content-Type': 'application/json' };
  }

  async function api(path, opts) {
    const url = cfg.apiUrl.replace(/\/+$/, '') + path;
    const init = Object.assign({ method: 'GET', headers: baseHeaders() }, opts || {});
    if (init.body && typeof init.body !== 'string') init.body = JSON.stringify(init.body);
    const r = await fetch(url, init);
    if (!r.ok) {
      const text = await r.text();
      let detail = text;
      try { detail = JSON.parse(text).error?.message || text; } catch (_) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('application/json')) return r.json();
    return r.text();
  }

  // SSE consumer over fetch + ReadableStream (works in all modern browsers and
  // supports POST with body, unlike EventSource).
  async function ssePost(path, body, onEvent) {
    if (abortController) abortController.abort();
    abortController = new AbortController();
    const url = cfg.apiUrl.replace(/\/+$/, '') + path;
    const r = await fetch(url, {
      method: 'POST',
      headers: baseHeaders(),
      body: JSON.stringify(body),
      signal: abortController.signal,
    });
    if (!r.ok) {
      const text = await r.text();
      let detail = text;
      try { detail = JSON.parse(text).error?.message || text; } catch (_) {}
      throw new Error(detail || ('HTTP ' + r.status));
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseSSEBlock(raw);
        if (ev) onEvent(ev);
      }
    }
    // tail
    if (buffer.trim()) {
      const ev = parseSSEBlock(buffer);
      if (ev) onEvent(ev);
    }
  }

  function parseSSEBlock(block) {
    let event = 'message';
    const dataLines = [];
    for (const line of block.split('\n')) {
      if (!line) continue;
      if (line.startsWith(':')) continue;       // comment
      const i = line.indexOf(':');
      const field = i === -1 ? line : line.slice(0, i);
      let value = i === -1 ? '' : line.slice(i + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') dataLines.push(value);
    }
    if (!dataLines.length && event === 'message') return null;
    const dataStr = dataLines.join('\n');
    let data;
    try { data = JSON.parse(dataStr); } catch (_) { data = dataStr; }
    return { event, data };
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------
  function ensureRoot() {
    if (rootEl) return rootEl;
    injectStyles();
    rootEl = el('div', {
      class: 'cs-root ' + (cfg.position === 'left' ? 'cs-pos-left' : 'cs-pos-right'),
      style: '--cs-accent: ' + cfg.accent,
    });

    buttonEl = el('button', {
      class: 'cs-btn',
      'aria-label': t('openAria'),
      onclick: toggle,
      html: chatBubbleIcon(),
    });

    panelEl = el('div', { class: 'cs-panel' });
    panelEl.appendChild(buildHeader());
    messagesEl = el('div', { class: 'cs-messages' });
    panelEl.appendChild(messagesEl);
    if (cfg.enableUpload) {
      panelEl.appendChild(buildUploadHint());
    }
    panelEl.appendChild(buildInput());

    rootEl.appendChild(buttonEl);
    rootEl.appendChild(panelEl);
    document.body.appendChild(rootEl);
    return rootEl;
  }

  function buildHeader() {
    return el('div', { class: 'cs-header' }, [
      buildAvatar(),
      el('div', { class: 'meta' }, [
        el('div', { class: 'title', id: 'cs-title' }, cfg.title),
        el('div', { class: 'subtitle', id: 'cs-subtitle' }, cfg.subtitle),
      ]),
      cfg.showLangSwitcher ? buildLangSwitcher() : null,
      el('button', { class: 'close', 'aria-label': t('closeAria'), onclick: close }, '×'),
    ]);
  }

  /** Globe button + dropdown listing the four languages in their own script. */
  function buildLangSwitcher() {
    const wrap = el('div', { class: 'cs-lang' });
    const btn = el('button', {
      class: 'cs-lang-btn',
      title: t('langTitle'),
      'aria-label': t('langTitle'),
      'aria-haspopup': 'true',
      html: globeIcon(),
      onclick: (e) => { e.stopPropagation(); toggleLangMenu(); },
    });
    langMenuEl = el('div', { class: 'cs-lang-menu' });
    LOCALE_LIST.forEach((loc) => {
      langMenuEl.appendChild(el('button', {
        class: loc.code === lang ? 'active' : '',
        lang: loc.htmlLang,
        onclick: (e) => { e.stopPropagation(); closeLangMenu(); setLang(loc.code); },
      }, loc.label));
    });
    wrap.appendChild(btn);
    wrap.appendChild(langMenuEl);
    return wrap;
  }

  function toggleLangMenu() {
    if (!langMenuEl) return;
    if (langMenuEl.classList.contains('open')) closeLangMenu();
    else {
      langMenuEl.classList.add('open');
      // Dismiss on the next outside click. `once` keeps us from stacking
      // listeners every time the menu opens.
      document.addEventListener('click', closeLangMenu, { once: true });
    }
  }

  function closeLangMenu() {
    if (langMenuEl) langMenuEl.classList.remove('open');
  }

  /** Avatar: an <img> when a URL is configured, otherwise the title's initial. */
  function buildAvatar() {
    const wrap = el('div', { class: 'avatar' });
    const url = (cfg.avatar || '').trim();
    const initial = (cfg.title || t('title')).slice(0, 1);
    if (url) {
      const img = el('img', { src: url, alt: cfg.title || t('title') });
      // Fall back to the initial if the image 404s or is blocked.
      img.addEventListener('error', () => {
        wrap.innerHTML = '';
        wrap.textContent = initial;
      });
      wrap.appendChild(img);
    } else {
      wrap.textContent = initial;
    }
    return wrap;
  }

  function buildInput() {
    const wrap = el('div', { class: 'cs-input' });
    if (cfg.enableUpload) {
      fileBtnEl = el('button', {
        class: 'cs-upload', title: t('uploadTitle'), 'aria-label': t('uploadTitle'),
        onclick: pickFile,
        // `html` (not children) — a string child becomes a text node, which
        // would print the SVG source instead of rendering it.
        html: paperclipIcon(),
      });
      fileInputEl = el('input', { type: 'file', accept: '.txt,.md,.markdown,.docx,.xlsx,.pdf', style: 'display:none', onchange: handleFileChosen });
      wrap.appendChild(fileBtnEl);
      wrap.appendChild(fileInputEl);
    }
    inputEl = el('textarea', { rows: '1', placeholder: cfg.placeholder, onkeydown: handleKey });
    wrap.appendChild(inputEl);
    wrap.appendChild(el('button', {
      class: 'cs-send', title: t('sendTitle'), 'aria-label': t('sendTitle'),
      onclick: () => sendCurrent(),
    }, '➤'));
    return wrap;
  }

  function buildUploadHint() {
    return el('div', { class: 'cs-upload-hint' }, cfg.uploadHint);
  }

  function appendMessage(role, text, sources) {
    if (!messagesEl) return null;
    const wrap = el('div', { class: 'cs-msg ' + role });
    const bubble = el('div', { class: 'bubble' });
    bubble.textContent = text;
    wrap.appendChild(bubble);
    if (sources && sources.length) {
      const det = el('details');
      const sum = el('summary', null, t('sources', { n: sources.length }));
      const ol = el('ol');
      sources.forEach(s => {
        const li = el('li');
        const head = s.filename ? `[${s.score || 0}] ${s.filename}` : `[${s.score || 0}] ${s.chunk_id}`;
        li.appendChild(document.createTextNode(head + ' — '));
        const code = el('span');
        code.textContent = (s.text || '').slice(0, 120) + (s.text && s.text.length > 120 ? '…' : '');
        li.appendChild(code);
        ol.appendChild(li);
      });
      det.appendChild(sum);
      det.appendChild(ol);
      const src = el('div', { class: 'cs-sources' });
      src.appendChild(det);
      wrap.appendChild(src);
    }
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { wrap, bubble };
  }

  function appendTyping(role) {
    const { wrap, bubble } = appendMessage(role, '');
    bubble.innerHTML = '<span class="cs-typing"><span></span><span></span><span></span></span>';
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return { wrap, bubble };
  }

  function chatBubbleIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
  }
  function paperclipIcon() {
    return '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>';
  }
  function globeIcon() {
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>';
  }

  // -------------------------------------------------------------------------
  // Behavior
  // -------------------------------------------------------------------------
  function open() {
    ensureRoot();
    panelEl.classList.add('open');
    isOpen = true;
    if (inputEl) inputEl.focus();
  }
  function close() {
    if (!panelEl) return;
    panelEl.classList.remove('open');
    isOpen = false;
  }
  function toggle() { isOpen ? close() : open(); }

  function handleKey(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendCurrent();
    }
  }

  function sendCurrent() {
    const text = (inputEl.value || '').trim();
    if (!text) return;
    inputEl.value = '';
    sendMessage(text);
  }

  /**
   * Render the finished answer: strip citation markers, then convert the
   * Markdown the model emits into real HTML.
   *
   * Streaming appends to textContent (XSS-safe, no reflow per token), so the
   * bubble holds raw Markdown until this runs. Everything is built with
   * createElement/textContent rather than innerHTML on model output, so a
   * malicious document in the knowledge base can't inject script.
   */
  function renderAnswer(bubble) {
    if (!bubble || bubble.dataset.rendered === '1') return;
    const raw = bubble.textContent || '';
    if (!raw.trim()) return;
    bubble.dataset.rendered = '1';

    // --- 1. Drop citation markers like [1], [2][3], 【1】 ---------------
    // `arr[0]` (code) and `2587.html[1]` (citation) are genuinely ambiguous.
    // We optimise for citations, which appear on most answers, over array
    // indexing, which is rare in customer-service replies.
    const text = raw
      .replace(/(?:[[［【]\s*\d+\s*[\]］】])+(?=\s|$|[，。；、！？）)])/g, '')
      .replace(/(?<=[一-鿿”"』」）)])(?:[[［【]\s*\d+\s*[\]］】])+/g, '')
      .replace(/[ \t]+([，。；、！？\n])/g, '$1')
      .replace(/\n{3,}/g, '\n\n')
      .trim();

    bubble.innerHTML = '';
    bubble.classList.add('md');
    renderMarkdown(bubble, text);
  }

  /** Inline formatting: **bold**, *italic*, `code`, links, bare URLs. */
  function renderInline(target, text) {
    // Bare-URL matching uses an ALLOW-list of RFC 3986 characters rather than
    // "anything but whitespace". A deny-list let CJK text run into the link:
    // ".../kecheng，免费分享课程" was swallowed whole because the Chinese comma
    // and the following characters weren't excluded. The trailing class also
    // drops sentence-final punctuation so "见 https://x.com/a.html。" keeps 。
    // outside the anchor.
    const URL_RE = "https?://[A-Za-z0-9\\-._~:/?#\\[\\]@!$&'()*+,;=%]*[A-Za-z0-9\\-_~/#@$&*+=%]";

    // Ordered by precedence; each alternative captures its own payload.
    const re = new RegExp([
      '`([^`]+)`',                                  // 1 code
      '\\*\\*([^*]+)\\*\\*',                        // 2 bold
      '__([^_]+)__',                                // 3 bold
      '(?<![\\w*])\\*([^*\\n]+)\\*(?![\\w*])',      // 4 italic
      '\\[([^\\]]+)\\]\\((https?://[^\\s)]+)\\)',   // 5,6 [text](url)
      `(${URL_RE})`,                                // 7 bare url
    ].join('|'), 'g');

    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) {
        target.appendChild(document.createTextNode(text.slice(last, m.index)));
      }
      if (m[1] !== undefined) {
        const c = document.createElement('code');
        c.textContent = m[1];
        target.appendChild(c);
      } else if (m[2] !== undefined || m[3] !== undefined) {
        const b = document.createElement('strong');
        b.textContent = m[2] !== undefined ? m[2] : m[3];
        target.appendChild(b);
      } else if (m[4] !== undefined) {
        const i = document.createElement('em');
        i.textContent = m[4];
        target.appendChild(i);
      } else if (m[5] !== undefined && m[6] !== undefined) {
        target.appendChild(makeLink(m[6], m[5]));
      } else if (m[7] !== undefined) {
        target.appendChild(makeLink(m[7], m[7]));
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) {
      target.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function makeLink(href, label) {
    const a = document.createElement('a');
    a.href = href;
    a.textContent = label;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'cs-link';
    return a;
  }

  /**
   * Block-level Markdown: headings, bullet/numbered lists, fenced code,
   * blockquotes, paragraphs. Deliberately small — just what an LLM answer
   * actually uses. No external dependency.
   */
  function renderMarkdown(root, text) {
    const lines = text.split('\n');
    let i = 0;
    let list = null;          // current <ul>/<ol> being filled

    const closeList = () => { list = null; };

    while (i < lines.length) {
      const line = lines[i];

      // Fenced code block
      const fence = line.match(/^\s*```\s*(\S*)\s*$/);
      if (fence) {
        closeList();
        const body = [];
        i++;
        while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) {
          body.push(lines[i]);
          i++;
        }
        i++;  // skip closing fence
        const pre = document.createElement('pre');
        const code = document.createElement('code');
        code.textContent = body.join('\n');
        pre.appendChild(code);
        root.appendChild(pre);
        continue;
      }

      // Heading: #..###### — rendered at a size that fits a chat bubble.
      const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
      if (h) {
        closeList();
        const el = document.createElement('div');
        el.className = 'cs-md-h cs-md-h' + Math.min(h[1].length, 4);
        renderInline(el, h[2].trim());
        root.appendChild(el);
        i++;
        continue;
      }

      // Blockquote
      const q = line.match(/^\s*>\s?(.*)$/);
      if (q) {
        closeList();
        const bq = document.createElement('blockquote');
        const parts = [q[1]];
        i++;
        while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
          parts.push(lines[i].replace(/^\s*>\s?/, ''));
          i++;
        }
        renderInline(bq, parts.join('\n'));
        root.appendChild(bq);
        continue;
      }

      // Horizontal rule
      if (/^\s*([-*_])\s*\1\s*\1[\s\S]*$/.test(line) && !/[^\s\-*_]/.test(line)) {
        closeList();
        root.appendChild(document.createElement('hr'));
        i++;
        continue;
      }

      // List item: -, *, • or 1. / 1)
      const li = line.match(/^(\s*)(?:[-*•]|(\d+)[.)])\s+(.*)$/);
      if (li) {
        const ordered = li[2] !== undefined;
        const wantTag = ordered ? 'OL' : 'UL';
        if (!list || list.tagName !== wantTag) {
          list = document.createElement(ordered ? 'ol' : 'ul');
          root.appendChild(list);
        }
        const item = document.createElement('li');
        // Continuation lines (indented, not a new item) belong to this item —
        // the model often puts a URL on its own indented line.
        const buf = [li[3]];
        i++;
        while (i < lines.length
               && /^\s{2,}\S/.test(lines[i])
               && !/^(\s*)(?:[-*•]|\d+[.)])\s+/.test(lines[i])) {
          buf.push(lines[i].trim());
          i++;
        }
        renderInline(item, buf.join('\n'));
        list.appendChild(item);
        continue;
      }

      // Blank line ends a list / paragraph
      if (!line.trim()) {
        closeList();
        i++;
        continue;
      }

      // Paragraph: gather until a blank line or a block-level marker
      closeList();
      const para = [line];
      i++;
      while (i < lines.length
             && lines[i].trim()
             && !/^\s*(#{1,6}\s|>|```|[-*•]\s|\d+[.)]\s)/.test(lines[i])) {
        para.push(lines[i]);
        i++;
      }
      const p = document.createElement('p');
      renderInline(p, para.join('\n'));
      root.appendChild(p);
    }
  }

  async function sendMessage(text) {
    ensureRoot();
    if (!isOpen) open();
    appendMessage('user', text);
    const placeholder = appendTyping('assistant');
    let gotAny = false;    // did we receive answer text?
    let finished = false;  // did the server send `done`?
    let failed = false;    // did we already show a failure notice?

    // A mid-stream failure must not erase the text that already arrived — it
    // is a real (and paid-for) partial answer, and the backend persists it
    // too. The notice goes in as its own block so it works whether or not the
    // bubble was already markdown-rendered (`renderAnswer` is one-shot).
    const failWith = (msg) => {
      if (failed) return;
      failed = true;
      if (gotAny) {
        renderAnswer(placeholder.bubble);
        const note = document.createElement('div');
        note.style.cssText = 'margin-top:6px;font-size:12px;opacity:.75;';
        note.textContent = '⚠ ' + msg;
        placeholder.bubble.appendChild(note);
      } else {
        placeholder.bubble.innerHTML = '';
        placeholder.bubble.textContent = '⚠ ' + msg;
      }
      messagesEl.scrollTop = messagesEl.scrollHeight;
    };

    try {
      // `lang` makes the backend answer in the current UI language even though
      // the knowledge base is Chinese. Sent per request, so switching mid-chat
      // takes effect on the very next answer.
      await ssePost('/api/chat/stream', { session_id: sessionId, message: text, lang: lang }, (ev) => {
        if (ev.event === 'meta' && ev.data && ev.data.session_id) {
          sessionId = ev.data.session_id;
        } else if (ev.event === 'token' && ev.data && ev.data.text) {
          if (placeholder.bubble.querySelector('.cs-typing')) {
            placeholder.bubble.innerHTML = '';
            placeholder.bubble.textContent = '';
          }
          gotAny = true;
          placeholder.bubble.textContent += ev.data.text;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (ev.event === 'sources') {
          // Sources arrive right before `done`, so the answer text is complete.
          renderAnswer(placeholder.bubble);
        } else if (ev.event === 'error') {
          const emsg = (ev.data && ev.data.message) || t('genericError');
          failWith(gotAny ? emsg + t('streamInterrupted') : emsg);
        } else if (ev.event === 'done') {
          finished = true;
          // Safety net: `sources` normally arrives first and triggers the
          // render, but it can be skipped (e.g. retrieval returned nothing).
          // renderAnswer() is idempotent, so calling it again is free.
          renderAnswer(placeholder.bubble);
          messagesEl.scrollTop = messagesEl.scrollHeight;
        }
      });
      // ssePost resolved but `done` never arrived: the connection was cut
      // mid-stream (proxy timeout, server restart). Say so instead of leaving
      // the bubble looking complete — or the typing dots spinning forever.
      if (!finished) failWith(t('streamInterrupted'));
    } catch (e) {
      failWith(gotAny ? e.message + t('streamInterrupted') : e.message);
    }
  }

  function pickFile() {
    if (fileInputEl) fileInputEl.click();
  }

  async function handleFileChosen(e) {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!f) return;
    ensureRoot();
    if (!isOpen) open();

    // appendMessage returns { wrap, bubble } — keep the bubble to update in place.
    const notice = appendMessage('system', t('uploading', {
      name: f.name, size: (f.size / 1024).toFixed(1),
    }));
    const say = (text) => { if (notice && notice.bubble) notice.bubble.textContent = text; };
    const base = cfg.apiUrl.replace(/\/+$/, '');

    try {
      const fd = new FormData();
      fd.append('file', f);
      const r = await fetch(base + '/api/documents/upload', { method: 'POST', body: fd });
      const upText = await r.text();
      let up;
      try { up = upText ? JSON.parse(upText) : null; } catch (_) { up = null; }
      if (!r.ok) {
        throw new Error((up && up.error && up.error.message) || upText || ('HTTP ' + r.status));
      }

      say(t('parsing', { name: up.filename }));

      const pr = await fetch(base + '/api/documents/process', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ document_id: up.id }),
      });
      const pdText = await pr.text();
      let pd;
      try { pd = pdText ? JSON.parse(pdText) : null; } catch (_) { pd = null; }

      if (!pr.ok) {
        throw new Error((pd && pd.error && pd.error.message) || pdText || ('HTTP ' + pr.status));
      }
      // /process answers 202 immediately and ingests in the background — the
      // outcome is in `status`, so checking pr.ok alone would report a bogus
      // success. 'processing' is the normal queued case; 'ready' means the
      // file was already ingested (duplicate upload); 'failed' is a fast
      // synchronous rejection (e.g. the parser failed before queueing).
      if (pd && pd.status === 'ready') {
        say(t('ingested', { name: up.filename, n: pd.chunk_count }));
      } else if (pd && pd.status === 'failed') {
        say(t('ingestFailed', {
          name: up.filename,
          err: pd.error_message || t('unknownError'),
        }));
      } else {
        say(t('ingesting', { name: up.filename }));
      }
    } catch (err) {
      say(t('uploadFailed', { err: err.message }));
    }
  }

  // -------------------------------------------------------------------------
  // Public init
  // -------------------------------------------------------------------------
  /**
   * Fetch 客服信息 from the backend and merge it in.
   *
   * Records the admin's values in `serverStrings` rather than writing straight
   * into cfg, so applyLocaleStrings() can re-evaluate the precedence chain
   * (init option > admin value > locale default) on every language switch.
   *
   * A value still equal to a shipped factory default is treated as "never
   * customised" — see FACTORY_VALUES for why that matters.
   */
  async function applyServerConfig() {
    try {
      const c = await fetchPublicConfig();
      if (!c) return;
      const fromServer = { title: c.name, avatar: c.avatar, welcome: c.welcome_message };
      for (const k in fromServer) {
        const v = fromServer[k];
        if (!v || explicitKeys.has(k)) continue;
        if (k === 'avatar') { cfg.avatar = v; continue; }   // not a translatable string
        const factory = FACTORY_VALUES[k] || [];
        if (factory.indexOf(String(v).trim()) === -1) serverStrings[k] = v;
      }
      // Surface contact details in the subtitle when the admin filled them in.
      // Phone/email are data, not language — no factory check needed.
      if (!explicitKeys.has('subtitle')) {
        const contact = [c.contact_phone, c.contact_email].filter(Boolean).join(' · ');
        if (contact) serverStrings.subtitle = contact;
      }
      applyLocaleStrings();
    } catch (_) {
      // Offline or CORS-blocked — keep the defaults, don't break the widget.
    }
  }

  /**
   * Read the visitor-safe slice of 客服信息.
   *
   * `/api/config/public` returns only name / avatar / welcome / contact and is
   * the endpoint to use: plain `/api/config` also carries API base URLs, model
   * names and vector-DB coordinates, and now answers 401 to anyone without an
   * admin token. The fallback exists purely so a newer widget keeps working
   * against a backend deployed before the public route was added — there the
   * old route is still open and still the only one that answers.
   */
  async function fetchPublicConfig() {
    try {
      return await api('/api/config/public');
    } catch (_) {
      return await api('/api/config');
    }
  }

  /** Re-render the header + welcome line after config or language changes. */
  function refreshHeader() {
    if (!panelEl) return;
    const old = panelEl.querySelector('.cs-header');
    if (old) panelEl.replaceChild(buildHeader(), old);
    // Replace the placeholder welcome text if it hasn't been talked over yet.
    if (messagesEl && messagesEl.children.length === 1 && cfg.welcome) {
      const bubble = messagesEl.querySelector('.cs-msg.assistant .bubble');
      if (bubble) bubble.textContent = cfg.welcome;
    }
  }

  /**
   * Re-render every piece of chrome that carries text.
   *
   * Existing messages are deliberately left alone: they were written in the
   * previous language and we cannot retranslate them. Only the *next* answer
   * changes language, which happens automatically because `lang` travels with
   * each request.
   */
  function refreshChrome() {
    applyLocaleStrings();
    refreshHeader();
    if (inputEl) inputEl.setAttribute('placeholder', cfg.placeholder);
    if (buttonEl) buttonEl.setAttribute('aria-label', t('openAria'));
    if (fileBtnEl) {
      fileBtnEl.setAttribute('title', t('uploadTitle'));
      fileBtnEl.setAttribute('aria-label', t('uploadTitle'));
    }
    if (panelEl) {
      const send = panelEl.querySelector('.cs-send');
      if (send) {
        send.setAttribute('title', t('sendTitle'));
        send.setAttribute('aria-label', t('sendTitle'));
      }
      const hint = panelEl.querySelector('.cs-upload-hint');
      if (hint) hint.textContent = cfg.uploadHint;
    }
  }

  /**
   * Switch language. Persists the choice, so it survives reloads and outranks
   * auto-detection from then on.
   */
  function setLang(code, opts) {
    const next = normalizeLang(code);
    if (!next || next === lang) return lang;
    lang = next;
    saveLang(next);
    refreshChrome();
    // Tell the visitor in the new language that the switch took, and that
    // answers will follow suit — otherwise a mid-chat switch looks like nothing
    // happened until they send another message.
    if (!(opts && opts.silent) && messagesEl && messagesEl.children.length) {
      appendMessage('system', t('switched'));
    }
    if (typeof cfg.onLangChange === 'function') {
      try { cfg.onLangChange(next); } catch (_) {}
    }
    return next;
  }

  function getLang() { return lang; }

  function init(options) {
    const opts = options || {};
    // Only keys with a real value count as "explicit" — `{title: null}` should
    // mean "use the locale default", not "render an empty header".
    explicitKeys = new Set(Object.keys(opts).filter(function (k) {
      return opts[k] !== null && opts[k] !== undefined && opts[k] !== '';
    }));
    cfg = Object.assign({}, DEFAULTS, opts);
    sessionId = cfg.sessionId || null;

    // 'auto' (the default) means detect; anything else is an explicit request
    // that still loses to a language the visitor picked by hand earlier.
    lang = detectLang(cfg.lang === 'auto' ? null : cfg.lang);
    applyLocaleStrings();

    const start = async () => {
      ensureRoot();
      if (messagesEl && !messagesEl.children.length && cfg.welcome) {
        appendMessage('assistant', cfg.welcome);
      }
      if (cfg.autoOpen) open();

      if (cfg.useServerConfig) {
        await applyServerConfig();
        refreshHeader();
      }

      if (typeof cfg.onReady === 'function') {
        try {
          cfg.onReady({ open, close, toggle, sendMessage, setLang, getLang, lang });
        } catch (_) {}
      }
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', start, { once: true });
    } else {
      start();
    }
  }

  function destroy() {
    if (rootEl && rootEl.parentNode) rootEl.parentNode.removeChild(rootEl);
    rootEl = panelEl = messagesEl = inputEl = buttonEl = fileBtnEl = fileInputEl = null;
    langMenuEl = null;
    if (abortController) abortController.abort();
    abortController = null;
    isOpen = false;
  }

  // Expose. setLang/getLang let the host page drive the language (e.g. an
  // existing site-wide switcher); resolveLang/LOCALE_LIST are exported so the
  // selftest can compare this inlined detector against shared/locale.js.
  window.CustomerService = {
    init, open, close, toggle, sendMessage, destroy,
    setLang, getLang, detectLang, resolveLang,
    locales: LOCALE_LIST,
  };
})();
