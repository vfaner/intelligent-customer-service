#!/usr/bin/env node
/**
 * Widget 自检：在真实 DOM 里跑一遍完整 SSE 流程。
 *
 * 为什么需要这个：`node -c` 只做语法检查，检不出「模板字符串被内部反引号
 * 截断」这类错误 —— 那会让整个 IIFE 在运行时抛错、widget 完全不工作，但
 * 静态检查一片绿。这个脚本用假 SSE 驱动真实渲染路径，任何破坏都会暴露。
 *
 * 用法：node selftest.mjs   （需要 jsdom：npm i -D jsdom）
 */
import { JSDOM } from 'jsdom';
import { readFileSync } from 'fs';
import { dirname, join } from 'path';
import { fileURLToPath } from 'url';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, 'customer-service.js'), 'utf8');

// Mirrors what the model actually produces, including the cases that have
// broken before: a URL followed by a Chinese comma + more text, a URL ending
// a sentence with 。, and trailing citation markers.
const ANSWER = `- **星空特效**：夜空背景动画，文章地址：https://www.qqmu.com/2497.html
- **流星雨**：用 \`canvas\` 实现，地址：https://www.qqmu.com/2569.html
- 编程课程栏目：https://www.qqmu.com/category/kecheng，免费分享一些编程相关的课程

### 更多
详见栏目：https://www.qqmu.com/category/texiao[1][2]
官网地址：https://www.qqmu.com/`;

function sseBody() {
  const out = [`event: meta\ndata: {"session_id":"s1"}\n\n`];
  for (const ch of ANSWER.match(/.{1,12}/gs)) {
    out.push(`event: token\ndata: ${JSON.stringify({ text: ch })}\n\n`);
  }
  out.push(`event: sources\ndata: [{"index":1,"chunk_id":"c1","text":"x","score":0.8}]\n\n`);
  out.push(`event: done\ndata: {"ok":true}\n\n`);
  return out.join('');
}

const dom = new JSDOM('<!DOCTYPE html><body></body>', {
  url: 'http://localhost/', runScripts: 'outside-only', pretendToBeVisual: true,
});
const { window } = dom;
// jsdom omits these globals; the widget's SSE reader and upload path need them.
window.TextDecoder = TextDecoder;
window.TextEncoder = TextEncoder;
window.AbortController = AbortController;
/** Every request body the widget sent, so we can assert `lang` travels along. */
const sentBodies = [];
window.fetch = async (url, opts) => {
  const u = String(url);
  if (opts && opts.body) { try { sentBodies.push(JSON.parse(opts.body)); } catch {} }
  if (u.includes('/api/chat/stream')) {
    const bytes = new TextEncoder().encode(sseBody());
    let sent = false;
    return { ok: true, status: 200, headers: { get: () => 'text/event-stream' },
      body: { getReader: () => ({ read: async () => (sent ? { done: true } : ((sent = true), { value: bytes, done: false })) }) } };
  }
  return { ok: false, status: 404, text: async () => '' };
};

// A broken template literal throws right here — that's the main thing we catch.
window.eval(src);
// `lang` is pinned so the assertions below don't depend on the machine's
// timezone. Detection itself is covered by the resolveLang case table.
// `enableUpload` is passed explicitly because it now ships off by default (the
// document endpoints require an admin token); the paperclip check below is
// about rendering, and the default itself is asserted separately.
window.CustomerService.init({
  apiUrl: 'http://x', useServerConfig: false, autoOpen: true, lang: 'en',
  enableUpload: true,
});
await new Promise(r => setTimeout(r, 150));
window.CustomerService.sendMessage('星空特效');
await new Promise(r => setTimeout(r, 1200));

const d = window.document;
const bubble = [...d.querySelectorAll('.cs-msg.assistant .bubble')].pop();
const checks = [
  ['widget 初始化',       () => !!d.querySelector('.cs-root')],
  ['CSS 完整注入',        () => (d.getElementById('cs-styles')?.textContent || '').includes('cs-md-h')],
  ['回形针为 SVG',        () => !!d.querySelector('.cs-upload svg')],
  // Visitor upload must stay opt-in: /api/documents/* answers 401 without an
  // admin token, so a shipped default of true would show a button that fails.
  ['访客上传默认关闭',    () => !/^\s*enableUpload:\s*true\s*,/m.test(src)],
  ['答案已渲染 (.md)',    () => bubble?.classList.contains('md')],
  ['链接转 <a>',          () => (bubble?.querySelectorAll('a.cs-link').length || 0) >= 3],
  ['href 有效',           () => bubble?.querySelector('a.cs-link')?.getAttribute('href')?.startsWith('http')],
  ['target=_blank',       () => bubble?.querySelector('a.cs-link')?.getAttribute('target') === '_blank'],
  ['**加粗** → strong',   () => !!bubble?.querySelector('strong')],
  ['`代码` → code',       () => !!bubble?.querySelector('code')],
  ['列表 → ul',           () => !!bubble?.querySelector('ul')],
  ['### → 标题',          () => !!bubble?.querySelector('.cs-md-h')],
  ['引用标记已清除',      () => !/\[\s*\d+\s*\]/.test(bubble?.textContent || '')],
  ['无旧引用面板',        () => !d.querySelector('.cs-sources')],
  // Regression: a deny-list URL pattern used to swallow the CJK text after a
  // link ("…/kecheng，免费分享…" became one giant anchor).
  ['链接不吞后续中文',    () => {
    const a = [...(bubble?.querySelectorAll('a.cs-link') || [])]
      .find(x => x.getAttribute('href')?.includes('/kecheng'));
    return a && a.getAttribute('href') === 'https://www.qqmu.com/category/kecheng';
  }],
  ['被吞的文字仍在气泡里', () => (bubble?.textContent || '').includes('免费分享一些编程相关的课程')],
  ['链接不含中文标点',    () => [...(bubble?.querySelectorAll('a.cs-link') || [])]
      .every(a => !/[，。、；！？一-鿿]/.test(a.getAttribute('href') || ''))],

  // Regression: the panel needs 540(h) + 76(button clearance) + 20(margin) = 636
  // tall and 360 + 20*2 = 400 wide. Hosts were sized 380x560, which clipped the
  // panel and let the iframe's own background show as a white block. Values are
  // read back from the CSS, so changing the panel size without updating the
  // embed docs / .chat-frame height fails here.
  ['iframe 尺寸要求仍是 400x636', () => {
    const num = (sel, prop) => {
      const i = src.indexOf(sel);
      const m = src.slice(i, src.indexOf('}', i))
                   .match(new RegExp(prop + '\\s*:\\s*(\\d+)px'));
      return m ? +m[1] : null;
    };
    const needH = num('.cs-pos-right {', 'bottom')
                + num('.cs-panel {', 'bottom')
                + num('.cs-panel {', 'height');
    const needW = num('.cs-panel {', 'width')
                + num('.cs-pos-right {', 'bottom') * 2;
    if (needH !== 636 || needW !== 400) {
      console.log(`\n  ⚠ 组件现在需要 ${needW}x${needH}，`
                + `请同步更新 embed 文档和 .chat-frame 高度`);
      return false;
    }
    return true;
  }],
];

// ---------------------------------------------------------------------------
// Language: detection priority, cross-copy drift, live switching
// ---------------------------------------------------------------------------
const CS = window.CustomerService;
// The widget inlines its own copy of the detector so it stays a single
// distributable file. shared/locale.js is the authoritative one — eval it here
// and assert the two never disagree.
window.eval(readFileSync(join(here, '..', 'shared', 'locale.js'), 'utf8'));
const shared = window.CSLocale;

// [input, expected locale, expected source]. Covers the requirement directly:
// 大陆→简中, 港澳台→繁中, 日本→日语, 其他→英语, plus every fallback rung.
const LOCALE_CASES = [
  [{},                                             'en',    'default'],
  [{ timeZone: 'Asia/Shanghai' },                  'zh-CN', 'timezone'],
  [{ timeZone: 'Asia/Urumqi' },                    'zh-CN', 'timezone'],
  [{ timeZone: 'PRC' },                            'zh-CN', 'timezone'],
  [{ timeZone: 'Asia/Taipei' },                    'zh-TW', 'timezone'],
  [{ timeZone: 'ROC' },                            'zh-TW', 'timezone'],
  [{ timeZone: 'Asia/Hong_Kong' },                 'zh-TW', 'timezone'],
  [{ timeZone: 'Hongkong' },                       'zh-TW', 'timezone'],
  [{ timeZone: 'Asia/Macau' },                     'zh-TW', 'timezone'],
  [{ timeZone: 'Asia/Macao' },                     'zh-TW', 'timezone'],
  [{ timeZone: 'Asia/Tokyo' },                     'ja',    'timezone'],
  [{ timeZone: 'Japan' },                          'ja',    'timezone'],
  [{ timeZone: 'Europe/Paris' },                   'en',    'default'],
  [{ timeZone: 'America/New_York' },               'en',    'default'],
  // Timezone beats browser language: a machine in Taipei on an English OS is
  // still in a Traditional-Chinese region.
  [{ timeZone: 'Asia/Taipei', languages: ['en-US'] },   'zh-TW', 'timezone'],
  // No region signal (e.g. TZ unset on a locked-down intranet box) → language.
  [{ languages: ['ja-JP', 'en'] },                 'ja',    'language'],
  [{ languages: ['zh-Hant-HK'] },                  'zh-TW', 'language'],
  [{ languages: ['zh-CHT'] },                      'zh-TW', 'language'],
  [{ languages: ['zh-CHS'] },                      'zh-CN', 'language'],
  [{ languages: ['zh-SG'] },                       'zh-CN', 'language'],
  [{ languages: ['yue'] },                         'zh-TW', 'language'],
  [{ languages: ['yue-Hans'] },                    'zh-CN', 'language'],
  // Unmatched tags must not short-circuit to English — keep scanning the list.
  [{ languages: ['fr-FR', 'de', 'zh-CN'] },        'zh-CN', 'language'],
  [{ languages: ['fr-FR', 'de'] },                 'en',    'default'],
  // An explicit request (init option / URL ?lang=) outranks even a stored
  // hand-pick: localStorage is shared per origin, one page's choice must not
  // override another page's explicit instruction.
  [{ stored: 'ja', explicit: 'en', timeZone: 'Asia/Shanghai' }, 'en',    'explicit'],
  [{ explicit: 'zh-TW', timeZone: 'Asia/Tokyo' },  'zh-TW', 'explicit'],
  // With nothing explicit, the hand-picked language outranks the region.
  [{ stored: 'ja', timeZone: 'Asia/Shanghai' },    'ja',    'stored'],
  // Junk in localStorage degrades to the next signal instead of throwing.
  [{ stored: 'klingon', timeZone: 'Asia/Tokyo' },  'ja',    'timezone'],
  [{ stored: null, languages: null, timeZone: null }, 'en',  'default'],
];

const localeFails = [];
const driftFails = [];
for (const [input, wantLocale, wantSource] of LOCALE_CASES) {
  const label = JSON.stringify(input);
  let a, b;
  try { a = CS.resolveLang(input); } catch (e) { a = { locale: 'THREW: ' + e.message }; }
  try { b = shared.resolve(input); } catch (e) { b = { locale: 'THREW: ' + e.message }; }
  if (a.locale !== wantLocale || a.source !== wantSource) {
    localeFails.push(`${label} → ${a.locale}/${a.source}，应为 ${wantLocale}/${wantSource}`);
  }
  if (a.locale !== b.locale || a.source !== b.source) {
    driftFails.push(`${label} → widget ${a.locale}/${a.source} vs shared ${b.locale}/${b.source}`);
  }
}

// Switch to Japanese and confirm the chrome actually re-renders. Past messages
// stay as-written (we can't retranslate them); only new answers change.
const enTitle = d.getElementById('cs-title')?.textContent;
const answerBefore = bubble?.textContent;
const msgCountBefore = d.querySelectorAll('.cs-msg').length;
CS.setLang('ja');
const jaTitle = d.getElementById('cs-title')?.textContent;

checks.push(
  ['地区检测优先级正确', () => {
    if (localeFails.length) console.log('\n  ' + localeFails.join('\n  '));
    return !localeFails.length;
  }],
  ['内联副本与 shared/locale.js 一致', () => {
    if (driftFails.length) console.log('\n  ' + driftFails.join('\n  '));
    return !driftFails.length;
  }],
  ['四种语言都在下拉里',   () => d.querySelectorAll('.cs-lang-menu button').length === 4],
  ['下拉用各自的文字',     () => {
    const labels = [...d.querySelectorAll('.cs-lang-menu button')].map(b => b.textContent);
    return ['简体中文', '繁體中文', '日本語', 'English'].every(x => labels.includes(x));
  }],
  ['下拉项带 lang 属性',   () => [...d.querySelectorAll('.cs-lang-menu button')]
      .every(b => !!b.getAttribute('lang'))],
  ['显式 lang 生效',       () => !!enTitle && !/[一-鿿]/.test(enTitle)],
  ['切换后标题变日语',     () => jaTitle && jaTitle !== enTitle && /[ぁ-んァ-ヶ一-鿿]/.test(jaTitle)],
  ['切换后输入框提示变了', () => {
    const ph = d.querySelector('.cs-input textarea')?.getAttribute('placeholder') || '';
    return /[ぁ-んァ-ヶ一-鿿]/.test(ph);
  }],
  ['切换后当前项高亮',     () => d.querySelector('.cs-lang-menu button.active')?.textContent === '日本語'],
  ['切换有系统提示',       () => d.querySelectorAll('.cs-msg').length === msgCountBefore + 1
      && !!d.querySelector('.cs-msg.system')],
  // Retranslating past turns would be a lie — they were written in the old
  // language and we have no translation for them.
  ['历史消息未被改写',     () => bubble?.textContent === answerBefore],
  ['getLang 反映切换',     () => CS.getLang() === 'ja'],
  ['切换已持久化',         () => window.localStorage.getItem('cs_lang') === 'ja'],
  ['请求带上了 lang',      () => {
    const chat = sentBodies.filter(b => 'message' in b).pop();
    return !!chat && chat.lang === 'en';   // pinned at init, before the switch
  }],
);

let failed = 0;
for (const [name, fn] of checks) {
  let ok = false;
  try { ok = !!fn(); } catch { ok = false; }
  if (!ok) failed++;
  console.log(`  ${ok ? '✓' : '✗'} ${name}`);
}
if (failed) {
  console.log('\n--- 气泡实际内容 ---');
  console.log((bubble?.innerHTML || '(空)').slice(0, 600));
}
console.log(failed ? `\n❌ ${failed}/${checks.length} 项失败` : `\n✅ ${checks.length} 项全部通过`);
process.exit(failed ? 1 : 0);
