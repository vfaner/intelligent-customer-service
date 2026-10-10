#!/usr/bin/env node
/**
 * 登录页自检：在真实 DOM 里跑一遍 login.html 的逻辑。
 *
 * 为什么需要这个：后端的 test_auth.py 能证明接口对，i18n-check.mjs 能证明文案齐，
 * 但登录页是整个后台的入口，它自己的那段脚本没有任何覆盖 —— 而里面有一个安全
 * 控制（`next` 参数的开放重定向防护）光靠读代码不容易确认对不对：`//evil.com`
 * 这种「协议相对 URL」看起来像站内路径，浏览器却会当成跨站跳转。
 *
 * 用法：node login-selftest.mjs
 *
 * jsdom 从 ../widget/node_modules 借用（widget 已经把它列为 devDependency）。
 * 先跑一次 `cd ../widget && npm i`。
 *
 * 实现说明：jsdom 的 `location.replace` 是只读的自有属性，改不掉，而页面成功登录
 * 后就是调它跳转的。所以这里不让 jsdom 自己执行页面脚本，而是把内联脚本抠出来包进
 * 一个以 `location` 为形参的函数里再 eval —— 形参遮蔽掉全局的 `location`，跳转目标
 * 就变成了可断言的值。其余全局（document / localStorage / fetch）都还是真的。
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

const html = readFileSync(join(here, 'login.html'), 'utf8');

// The page's own inline script, to be re-evaluated under a shadowed `location`.
const INLINE = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map((m) => m[1]).join('\n');
if (!/api\/auth\/login/.test(INLINE)) {
  console.error('✗ 没抠到 login.html 的内联脚本 —— 正则和页面结构不一致了');
  process.exit(1);
}

/**
 * Load login.html with a stubbed backend, and return what it tried to do.
 *
 * External <script src> tags are never fetched, so window.CSAdminI18n is absent
 * — which is exactly the fallback path the page is written to survive, and one
 * of the things worth asserting.
 */
async function load({ search = '', state = null, loginResult = null } = {}) {
  const dom = new JSDOM(html, {
    url: 'http://localhost:8000/admin/login.html' + search,
    // 'outside-only': give us window.eval but don't run the page's scripts, so
    // we can install fetch and shadow location first.
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;

  const calls = [];
  window.fetch = async (url, init) => {
    calls.push({ url: String(url), init: init || {} });
    if (String(url).endsWith('/api/auth/state')) {
      if (!state) return { ok: false, status: 500, json: async () => null, text: async () => '' };
      return { ok: true, status: 200, json: async () => state, text: async () => JSON.stringify(state) };
    }
    if (String(url).endsWith('/api/auth/login')) {
      const r = loginResult
        || { status: 200, body: { access_token: 'tok-abc', user: { username: 'admin' } } };
      return {
        ok: r.status < 400, status: r.status,
        text: async () => JSON.stringify(r.body),
        json: async () => r.body,
      };
    }
    throw new Error('unexpected fetch: ' + url);
  };

  const replaced = [];
  window.__fakeLocation = {
    search: search,
    origin: 'http://localhost:8000',
    href: 'http://localhost:8000/admin/login.html' + search,
    replace: (u) => replaced.push(u),
  };
  // A broken template literal or stray syntax error in login.html throws here.
  window.eval('(function (location) {\n' + INLINE + '\n})(window.__fakeLocation);');

  // Let the /api/auth/state promise settle.
  await new Promise((r) => setTimeout(r, 60));
  return { dom, window, calls, replaced };
}

const submit = async (window) => {
  window.document.getElementById('loginForm')
    .dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
  await new Promise((r) => setTimeout(r, 60));
};

const results = [];
const check = (name, pass) => results.push([name, !!pass]);

// --- 1. 页面在没有 i18n.js 的情况下也能起来 -----------------------------------
{
  const { window, calls } = await load({ state: { auth_required: true, default_password: false } });
  check('表单渲染出来了', !!window.document.getElementById('loginForm'));
  check('缺少 i18n.js 时不崩', !window.CSAdminI18n && !!window.document.getElementById('loginBtn'));
  check('开局就问了 /api/auth/state', calls.some((c) => c.url.endsWith('/api/auth/state')));
  check('默认密码已改时不显示提示',
    !window.document.getElementById('defaultHint').classList.contains('show'));
}

// --- 2. 默认密码提示只在还没改密码时出现 -------------------------------------
{
  const { window } = await load({ state: { auth_required: true, default_password: true } });
  const hint = window.document.getElementById('defaultHint');
  check('还在用默认密码时显示提示', hint.classList.contains('show'));
  check('提示里给了默认账号密码', /admin/.test(hint.textContent) && /123456/.test(hint.textContent));
}

// --- 3. 关掉鉴权时说清楚，而不是假装要密码 -----------------------------------
{
  const { window } = await load({ state: { auth_required: false, default_password: true } });
  const hint = window.document.getElementById('defaultHint');
  check('AUTH_ENABLED=false 时给出说明', hint.classList.contains('show')
    && /AUTH_ENABLED/.test(hint.textContent));
}

// --- 4. 登录成功：存令牌 + 跳转 ----------------------------------------------
{
  const { window, calls, replaced } = await load({ state: { auth_required: true, default_password: true } });
  window.document.getElementById('username').value = 'admin';
  window.document.getElementById('password').value = '123456';
  await submit(window);

  const login = calls.find((c) => c.url.endsWith('/api/auth/login'));
  check('POST 到了 /api/auth/login', login && login.init.method === 'POST');
  check('带上了 credentials（cookie 用于页面拦截）', login && login.init.credentials === 'include');
  check('请求体里是填的账号密码', login && /"username":"admin"/.test(login.init.body));
  check('令牌存进了 localStorage', window.localStorage.getItem('cs_admin_token') === 'tok-abc');
  check('用户信息也存了', /admin/.test(window.localStorage.getItem('cs_admin_user') || ''));
  check('跳转回了后台首页', replaced[0] === '/admin/');
}

// --- 5. 登录失败：报错、不跳转、不存令牌 -------------------------------------
{
  const { window, replaced } = await load({
    state: { auth_required: true, default_password: true },
    loginResult: { status: 401, body: { error: { code: 'unauthorized', message: '用户名或密码错误' } } },
  });
  window.document.getElementById('username').value = 'admin';
  window.document.getElementById('password').value = 'wrong';
  await submit(window);

  const err = window.document.getElementById('loginError');
  check('显示了后端返回的错误', err.classList.contains('show') && /密码错误/.test(err.textContent));
  check('失败时不存令牌', !window.localStorage.getItem('cs_admin_token'));
  check('失败时不跳转', replaced.length === 0);
  check('按钮恢复可点（不会卡在「登录中…」）',
    !window.document.getElementById('loginBtn').disabled);
}

// --- 6. 空表单不发请求 --------------------------------------------------------
{
  const { window, calls } = await load({ state: { auth_required: true, default_password: true } });
  await submit(window);
  check('用户名密码为空时不发请求', !calls.some((c) => c.url.endsWith('/api/auth/login')));
  check('并且给了提示', window.document.getElementById('loginError').classList.contains('show'));
}

// --- 7. next 的开放重定向防护 -------------------------------------------------
// 这是本文件存在的主要理由。`/admin/` 这类站内路径要放行，其余一律回后台首页。
{
  const cases = [
    ['/admin/',                    '/admin/'],
    ['/admin/index.html',          '/admin/index.html'],
    // 协议相对 URL：浏览器会跳到 evil.com，虽然它以 / 开头
    ['//evil.com/',                '/admin/'],
    ['//evil.com',                 '/admin/'],
    // 反斜杠会被浏览器规范化成 /，'/\evil.com' 等价于 '//evil.com'
    ['/\\evil.com/',               '/admin/'],
    // 浏览器还会从 URL 里剥掉制表/换行符，剥完可能拼出 '//host'
    ['/\t//evil.com',              '/admin/'],
    ['/\n/evil.com',               '/admin/'],
    ['https://evil.com/',          '/admin/'],
    ['http://evil.com/',           '/admin/'],
    ['javascript:alert(1)',        '/admin/'],
    ['evil.com',                   '/admin/'],
    ['',                           '/admin/'],
  ];
  for (const [next, want] of cases) {
    const { window, replaced } = await load({
      search: '?next=' + encodeURIComponent(next),
      state: { auth_required: true, default_password: true },
    });
    window.document.getElementById('username').value = 'admin';
    window.document.getElementById('password').value = '123456';
    await submit(window);
    check(`next=${JSON.stringify(next)} → ${want}`, replaced[0] === want);
  }
}

// --- 8. ?api= 指向另一个后端时，请求跟着走 -----------------------------------
{
  const { calls, window } = await load({
    search: '?api=' + encodeURIComponent('https://api.example.com/'),
    state: { auth_required: true, default_password: true },
  });
  check('state 请求打到了 ?api= 指定的后端',
    calls.some((c) => c.url === 'https://api.example.com/api/auth/state'));
  check('末尾斜杠被去掉，没有出现 //api',
    !calls.some((c) => /\/\/api\/auth/.test(c.url.replace(/^https?:\/\//, ''))));
  check('?api= 被记住，供后台页面复用',
    window.localStorage.getItem('cs_admin_api') === 'https://api.example.com');
}

// --- 9. 后端连不上时报得像句人话 ----------------------------------------------
{
  const { window } = await load({ state: null });   // /api/auth/state 500
  const dom = window.document;
  dom.getElementById('username').value = 'admin';
  dom.getElementById('password').value = '123456';
  window.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await submit(window);
  const err = dom.getElementById('loginError');
  check('后端挂了时给出可读的提示',
    err.classList.contains('show') && /无法连接后端|Failed to fetch/.test(err.textContent));
}

// --- 10. AUTH_ENABLED=false 时后台不能把人踢到登录页 -------------------------
// 这是「我把自己锁在外面了」的逃生口。后端中间件放行了 /admin/，如果 index.html
// 的脚本还是照样跳登录页，用户就在两个页面之间弹来弹去，永远进不去。
{
  const consoleHtml = readFileSync(join(here, 'index.html'), 'utf8');
  const src = [...consoleHtml.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => m[1]).join('\n');
  const guard = /async function requireSession\(\)[\s\S]*?\n}/.exec(src);
  check('后台里有 requireSession()', !!guard);
  if (guard) {
    // Run the guard alone, against a fake API + AUTH, rather than booting the
    // whole console: everything else in there wants a live backend.
    const run = async ({ token, state, throws }) => {
      const bounced = [];
      const fn = new Function('AUTH', 'API', 'fetch', guard[0] + '\nreturn requireSession();');
      return {
        ok: await fn(
          { token: () => token, toLogin: () => bounced.push(1) },
          { url: 'http://x' },
          async () => {
            if (throws) throw new TypeError('Failed to fetch');
            return { ok: !!state, json: async () => state };
          },
        ),
        bounced: bounced.length,
      };
    };
    let r = await run({ token: 'tok', state: { auth_required: true } });
    check('有令牌时直接放行，不问后端', r.ok && r.bounced === 0);

    r = await run({ token: null, state: { auth_required: true } });
    check('没令牌 + 要求登录 → 跳登录页', !r.ok && r.bounced === 1);

    r = await run({ token: null, state: { auth_required: false } });
    check('没令牌 + AUTH_ENABLED=false → 放行（逃生口可用）', r.ok && r.bounced === 0);

    r = await run({ token: null, throws: true });
    check('后端连不上时跳登录页（那边会说清原因）', !r.ok && r.bounced === 1);
  }
}

// --- report -------------------------------------------------------------------
let failed = 0;
for (const [name, pass] of results) {
  console.log(`  ${pass ? '✓' : '✗'} ${name}`);
  if (!pass) failed++;
}
console.log('');
if (failed) {
  console.log(`❌ ${failed}/${results.length} 项失败`);
  process.exit(1);
}
console.log(`✅ ${results.length} 项全部通过`);
