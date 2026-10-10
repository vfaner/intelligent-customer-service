/*!
 * CSLocale — 离线地区/语言识别（简体 · 繁体 · 日语 · 英语）
 *
 * 为什么不用 IP 地理库：目标部署环境是内网/离线。内网 IP（10.x / 192.168.x）
 * 本身不携带地域信息，任何在线 geo API 也打不通。所以只用两个纯客户端信号：
 *
 *   1. 系统时区  Intl.DateTimeFormat().resolvedOptions().timeZone  ← 判「地区」
 *   2. 浏览器语言 navigator.languages                              ← 判「语言偏好」
 *
 * 时区优先，因为需求是「根据所在地区自动选择」：一台在台北、系统装英文的电脑
 * 时区仍是 Asia/Taipei，应当给繁体中文，而 navigator.language 会说 en。
 *
 * 这份文件是检测逻辑的**唯一权威实现**。widget（customer-service.js）为了保持
 * 单文件可分发，内联了一份副本；selftest.mjs 会拿同一张输入表比对两者的
 * resolve() 结果，任何漂移都会让测试失败。改这里请同步改 widget。
 */
(function (root) {
  'use strict';

  var STORAGE_KEY = 'cs_lang';
  var DEFAULT_LOCALE = 'en';

  /** 支持的语言 + 展示元数据。label 用各自母语书写，切换菜单里才认得出。 */
  var LOCALES = [
    { code: 'zh-CN', label: '简体中文', htmlLang: 'zh-CN' },
    { code: 'zh-TW', label: '繁體中文', htmlLang: 'zh-TW' },
    { code: 'ja',    label: '日本語',   htmlLang: 'ja'    },
    { code: 'en',    label: 'English',  htmlLang: 'en'    }
  ];

  var SUPPORTED = LOCALES.map(function (l) { return l.code; });

  /**
   * IANA 时区 → 语言。
   *
   * 同时收录 canonical 名和 legacy 别名：现代浏览器会把 PRC / ROC / Hongkong /
   * Japan / Asia/Macao 规范化成 Asia/Shanghai 等（已实测），但老浏览器可能原样
   * 返回别名，多几个 key 的成本是零。
   *
   * 中国大陆含新疆（Asia/Urumqi）—— 同属大陆，用简体。
   * 港澳台三地统一繁体，符合需求。
   */
  var TZ_TO_LOCALE = {
    // 中国大陆 → 简体中文
    'Asia/Shanghai': 'zh-CN',
    'Asia/Chongqing': 'zh-CN',
    'Asia/Chungking': 'zh-CN',
    'Asia/Harbin': 'zh-CN',
    'Asia/Urumqi': 'zh-CN',
    'Asia/Kashgar': 'zh-CN',
    'PRC': 'zh-CN',
    // 台湾 / 香港 / 澳门 → 繁体中文
    'Asia/Taipei': 'zh-TW',
    'ROC': 'zh-TW',
    'Asia/Hong_Kong': 'zh-TW',
    'Hongkong': 'zh-TW',
    'Asia/Macau': 'zh-TW',
    'Asia/Macao': 'zh-TW',
    // 日本 → 日语
    'Asia/Tokyo': 'ja',
    'Japan': 'ja'
  };

  /** 语言标签里代表「繁体」的 script / region 子标签（含微软的 zh-CHT）。 */
  var TRADITIONAL_SUBTAGS = { hant: 1, cht: 1, tw: 1, hk: 1, mo: 1 };
  /** 代表「简体」的子标签 —— yue-Hans（简体粤语）要靠它才能判对。 */
  var SIMPLIFIED_SUBTAGS = { hans: 1, chs: 1, cn: 1, sg: 1, my: 1 };

  function isSupported(code) {
    return SUPPORTED.indexOf(code) !== -1;
  }

  /** 规范化外部传入的语言代码（URL 参数、init 选项、localStorage）。 */
  function normalize(code) {
    if (!code) return null;
    var raw = String(code).trim();
    if (isSupported(raw)) return raw;
    // 宽松匹配：zh_TW / ZH-tw / zh-Hant 之类也接受
    var guess = fromLanguageTag(raw);
    return guess && isSupported(guess) ? guess : null;
  }

  /** 时区 → 语言；未收录返回 null（交给语言兜底）。 */
  function fromTimeZone(tz) {
    if (!tz) return null;
    return TZ_TO_LOCALE[tz] || null;
  }

  /**
   * 单个 BCP-47 标签 → 语言；无法判断返回 null（继续扫下一条）。
   *
   * 返回 null 而不是直接给 'en' 是刻意的：navigator.languages 是有序偏好列表，
   * ['fr-FR', 'zh-CN'] 里法语无对应语言，应该继续看 zh-CN，而不是停在英语。
   */
  function fromLanguageTag(tag) {
    if (!tag) return null;
    var parts = String(tag).toLowerCase().replace(/_/g, '-').split('-');
    var primary = parts[0];

    if (primary === 'ja') return 'ja';
    if (primary === 'en') return 'en';

    // zh 以及 yue（粤语）：靠 script/region 子标签区分简繁。
    if (primary === 'zh' || primary === 'yue') {
      for (var i = 1; i < parts.length; i++) {
        if (TRADITIONAL_SUBTAGS[parts[i]]) return 'zh-TW';
      }
      for (var j = 1; j < parts.length; j++) {
        if (SIMPLIFIED_SUBTAGS[parts[j]]) return 'zh-CN';
      }
      // 没有简繁线索时：粤语默认繁体（yue-Hant-HK 是最常见形式），
      // 裸 zh 默认简体。
      return primary === 'yue' ? 'zh-TW' : 'zh-CN';
    }
    return null;
  }

  /** 有序偏好列表 → 第一个能判定的语言。 */
  function fromLanguages(list) {
    if (!list) return null;
    var arr = typeof list === 'string' ? [list] : list;
    for (var i = 0; i < arr.length; i++) {
      var hit = fromLanguageTag(arr[i]);
      if (hit) return hit;
    }
    return null;
  }

  /**
   * 纯函数解析器 —— 所有输入显式传入，便于测试。
   *
   * 优先级：显式指定 > 手动选择 > 时区(地区) > 浏览器语言 > 英语
   *
   * 显式指定（URL ?lang= / init({lang})）表达的是「这一次访问」的意图，
   * 压过 localStorage 里的手动选择：存储在同源下是共享的，A 页面的选择
   * 不应悄悄盖掉 B 页面明确的指定。没有显式指定时，手动选择仍然优先于
   * 一切自动检测。
   *
   * @param {object} input
   * @param {string} [input.stored]      localStorage 里的手动选择
   * @param {string} [input.explicit]    URL ?lang= 或 init({lang})
   * @param {string} [input.timeZone]    IANA 时区名
   * @param {string[]} [input.languages] navigator.languages
   * @returns {{locale: string, source: string}} source 便于调试/日志
   */
  function resolve(input) {
    var opts = input || {};

    var explicit = normalize(opts.explicit);
    if (explicit) return { locale: explicit, source: 'explicit' };

    var stored = normalize(opts.stored);
    if (stored) return { locale: stored, source: 'stored' };

    var byTz = fromTimeZone(opts.timeZone);
    if (byTz) return { locale: byTz, source: 'timezone' };

    var byLang = fromLanguages(opts.languages);
    if (byLang) return { locale: byLang, source: 'language' };

    return { locale: DEFAULT_LOCALE, source: 'default' };
  }

  // --- 浏览器环境读取 ------------------------------------------------------

  function currentTimeZone() {
    // 老浏览器没有 Intl / resolvedOptions().timeZone —— 返回 undefined，
    // resolve() 会自动落到语言兜底，不抛错。
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || null;
    } catch (_) {
      return null;
    }
  }

  function currentLanguages() {
    var nav = root.navigator || {};
    if (nav.languages && nav.languages.length) return nav.languages;
    var one = nav.language || nav.userLanguage;
    return one ? [one] : [];
  }

  function read() {
    try {
      return root.localStorage ? root.localStorage.getItem(STORAGE_KEY) : null;
    } catch (_) {
      return null;   // 隐私模式 / 第三方 Cookie 被禁
    }
  }

  function save(code) {
    var ok = normalize(code);
    if (!ok) return null;
    try {
      if (root.localStorage) root.localStorage.setItem(STORAGE_KEY, ok);
    } catch (_) { /* 存不下也要能用，只是刷新后回到自动检测 */ }
    return ok;
  }

  function clear() {
    try {
      if (root.localStorage) root.localStorage.removeItem(STORAGE_KEY);
    } catch (_) {}
  }

  /** 从 URL 查询串取 ?lang=（供 embed / demo 页透传）。 */
  function fromUrl(search) {
    var qs = search !== undefined ? search
           : (root.location ? root.location.search : '');
    if (!qs) return null;
    try {
      return new URLSearchParams(qs).get('lang');
    } catch (_) {
      var m = String(qs).match(/[?&]lang=([^&]+)/);
      return m ? decodeURIComponent(m[1]) : null;
    }
  }

  /** 真实环境下的一次性检测。 */
  function detect(opts) {
    var o = opts || {};
    return resolve({
      stored: o.stored !== undefined ? o.stored : read(),
      explicit: o.explicit !== undefined ? o.explicit : fromUrl(),
      timeZone: o.timeZone !== undefined ? o.timeZone : currentTimeZone(),
      languages: o.languages !== undefined ? o.languages : currentLanguages()
    });
  }

  /** 同步 <html lang> —— 影响断行、字体回退和屏幕阅读器发音。 */
  function applyHtmlLang(code, doc) {
    var d = doc || root.document;
    if (!d || !d.documentElement) return;
    for (var i = 0; i < LOCALES.length; i++) {
      if (LOCALES[i].code === code) {
        d.documentElement.setAttribute('lang', LOCALES[i].htmlLang);
        return;
      }
    }
  }

  root.CSLocale = {
    STORAGE_KEY: STORAGE_KEY,
    DEFAULT_LOCALE: DEFAULT_LOCALE,
    LOCALES: LOCALES,
    SUPPORTED: SUPPORTED,
    TZ_TO_LOCALE: TZ_TO_LOCALE,
    isSupported: isSupported,
    normalize: normalize,
    fromTimeZone: fromTimeZone,
    fromLanguageTag: fromLanguageTag,
    fromLanguages: fromLanguages,
    resolve: resolve,
    detect: detect,
    read: read,
    save: save,
    clear: clear,
    fromUrl: fromUrl,
    applyHtmlLang: applyHtmlLang
  };
})(typeof window !== 'undefined' ? window : globalThis);
