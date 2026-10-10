"""FastAPI application factory.

Three equivalent ways to start the server::

    python run.py                          # 推荐：项目根的一键启动脚本
    python src/main.py                     # 直接跑本文件（PyCharm ▶ 也可以）
    uvicorn src.main:app --reload          # 标准 uvicorn 命令

All routers live under ``/api/*``. CORS is permissive by default so the
embeddable widget can be loaded from any third-party site.
"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

# 支持 `python src/main.py` 直接运行：把 backend/ 加进 sys.path，
# 这样下面的 `src.xxx` 绝对导入才能找到包。
if __package__ in (None, ""):
    _BACKEND_DIR = Path(__file__).resolve().parent.parent
    if str(_BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(_BACKEND_DIR))

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.api import (
    admin as admin_api,
    auth as auth_api,
    chat,
    config,
    documents,
    models as models_api,
    sessions,
)
from src.core.auth import require_admin
from src.core.config import get_settings
from src.core.database import dispose_engine, get_engine, session_scope
from src.core.db_drivers import DatabaseDriverError
from src.core.exceptions import install_exception_handlers
from src.core import security
from src.core.security import TokenError, decode_token
from src.models.base import Base
from src.services import runtime
from src.services.auth_service import ensure_default_admin

log = logging.getLogger(__name__)


# The landing page below is a Chinese f-string; this block translates it in the
# browser instead. Kept as a plain (non-f) string so the JS braces don't need
# doubling, and kept inline rather than in a .js file so the page keeps working
# even if the static mounts are ever moved.
#
# The msgid is the Chinese source text itself — same convention as the admin
# console (frontend/admin/i18n.js), so zh-CN needs no catalog at all and a
# missing entry degrades to Chinese rather than to a raw key.
_LANDING_I18N_SCRIPT = r"""
<script src="/shared/locale.js"></script>
<script>
(function () {
  var CAT = {
    'zh-TW': {
      '🚀 快速开始': '🚀 快速開始',
      '第一次使用？打开 <a href="/admin/"><strong>管理后台</strong></a>，填入 API Key 即可。':
        '第一次使用？打開 <a href="/admin/"><strong>管理後台</strong></a>，填入 API Key 即可。',
      '⚙️ 进入管理后台': '⚙️ 進入管理後台',
      '🪟 客服组件演示': '🪟 客服元件示範',
      '📡 API 端点': '📡 API 端點',
      '后台登录': '後台登入',
      'SSE 流式问答': 'SSE 串流問答',
      '测试模型连接': '測試模型連線',
      '完整列表见 <a href="/docs">Swagger 文档</a>（共 34 个端点）':
        '完整清單見 <a href="/docs">Swagger 文件</a>（共 34 個端點）',
      '🌐 嵌入到你的网站': '🌐 嵌入到你的網站',
      '客服名称、头像、欢迎语不用写在这里 —— 组件会自动读取 <a href="/admin/">「客服信息」</a>的配置，改一次对所有站点生效。<br> <code>?v=1</code> 是缓存版本号，更新组件后递增它。':
        '客服名稱、頭像、歡迎語不用寫在這裡 —— 元件會自動讀取 <a href="/admin/">「客服資訊」</a>的設定，改一次對所有站點生效。<br> <code>?v=1</code> 是快取版本號，更新元件後遞增它。',
      '更多方式（iframe / 小程序 / 桌面端）见 <a href="/widget/demo/embed.html">嵌入演示</a>':
        '更多方式（iframe / 小程式 / 桌面端）見 <a href="/widget/demo/embed.html">嵌入示範</a>',
      '🚀 部署到服务器': '🚀 部署到伺服器',
      '本机跑通后，把它部署到自己的服务器对外提供服务：':
        '在本機跑通後，把它部署到自己的伺服器對外提供服務：',
      '<b>最快</b>：Docker Compose 一条命令起全套（应用 + MySQL + Redis + 向量库）':
        '<b>最快</b>：Docker Compose 一條命令起全套（應用 + MySQL + Redis + 向量庫）',
      '<b>常规</b>：systemd 托管 + Nginx 反向代理 + HTTPS 证书':
        '<b>常規</b>：systemd 託管 + Nginx 反向代理 + HTTPS 憑證',
      '<b>注意</b>：SSE 流式问答需要在 Nginx 关闭缓冲，否则回答不会逐字出现':
        '<b>注意</b>：SSE 串流問答需要在 Nginx 關閉緩衝，否則回答不會逐字出現',
      '完整步骤（含 Nginx 配置、HTTPS、防火墙、备份、安全加固）见 <b>项目根目录的 <code>docs/DEPLOYMENT.md</code></b>。':
        '完整步驟（含 Nginx 設定、HTTPS、防火牆、備份、安全強化）見 <b>專案根目錄的 <code>docs/DEPLOYMENT.md</code></b>。'
    },
    'ja': {
      '🚀 快速开始': '🚀 クイックスタート',
      '第一次使用？打开 <a href="/admin/"><strong>管理后台</strong></a>，填入 API Key 即可。':
        '初めてお使いですか？<a href="/admin/"><strong>管理コンソール</strong></a>を開いて API キーを入力するだけです。',
      '⚙️ 进入管理后台': '⚙️ 管理コンソールを開く',
      '🪟 客服组件演示': '🪟 チャットウィジェットのデモ',
      '📡 API 端点': '📡 API エンドポイント',
      '后台登录': '管理画面ログイン',
      'SSE 流式问答': 'SSE ストリーミング応答',
      '测试模型连接': 'モデル接続のテスト',
      '完整列表见 <a href="/docs">Swagger 文档</a>（共 34 个端点）':
        '全一覧は <a href="/docs">Swagger ドキュメント</a>をご覧ください（全 34 エンドポイント）',
      '🌐 嵌入到你的网站': '🌐 自分のサイトに埋め込む',
      '客服名称、头像、欢迎语不用写在这里 —— 组件会自动读取 <a href="/admin/">「客服信息」</a>的配置，改一次对所有站点生效。<br> <code>?v=1</code> 是缓存版本号，更新组件后递增它。':
        '名前・アイコン・あいさつ文をここに書く必要はありません。ウィジェットが <a href="/admin/">「担当者情報」</a>の設定を自動で読み込むので、一度変更すればすべてのサイトに反映されます。<br> <code>?v=1</code> はキャッシュ用のバージョン番号です。ウィジェットを更新したら増やしてください。',
      '更多方式（iframe / 小程序 / 桌面端）见 <a href="/widget/demo/embed.html">嵌入演示</a>':
        'その他の方法（iframe / ミニプログラム / デスクトップ）は <a href="/widget/demo/embed.html">埋め込みデモ</a>をご覧ください',
      '🚀 部署到服务器': '🚀 サーバーへデプロイ',
      '本机跑通后，把它部署到自己的服务器对外提供服务：':
        'ローカルで動作を確認したら、自分のサーバーにデプロイして公開しましょう。',
      '<b>最快</b>：Docker Compose 一条命令起全套（应用 + MySQL + Redis + 向量库）':
        '<b>最速</b>：Docker Compose ならコマンド 1 つで一式（アプリ + MySQL + Redis + ベクトル DB）が起動します',
      '<b>常规</b>：systemd 托管 + Nginx 反向代理 + HTTPS 证书':
        '<b>標準</b>：systemd で常駐 + Nginx リバースプロキシ + HTTPS 証明書',
      '<b>注意</b>：SSE 流式问答需要在 Nginx 关闭缓冲，否则回答不会逐字出现':
        '<b>注意</b>：SSE ストリーミング応答には Nginx のバッファリング無効化が必要です。有効なままだと回答が 1 文字ずつ表示されません',
      '完整步骤（含 Nginx 配置、HTTPS、防火墙、备份、安全加固）见 <b>项目根目录的 <code>docs/DEPLOYMENT.md</code></b>。':
        '詳しい手順（Nginx 設定・HTTPS・ファイアウォール・バックアップ・セキュリティ強化）は <b>プロジェクト直下の <code>docs/DEPLOYMENT.md</code></b> にあります。'
    },
    'en': {
      '🚀 快速开始': '🚀 Quick start',
      '第一次使用？打开 <a href="/admin/"><strong>管理后台</strong></a>，填入 API Key 即可。':
        'First time here? Open the <a href="/admin/"><strong>admin console</strong></a> and paste in an API key — that is all it takes.',
      '⚙️ 进入管理后台': '⚙️ Open admin console',
      '🪟 客服组件演示': '🪟 Chat widget demo',
      '📡 API 端点': '📡 API endpoints',
      '后台登录': 'admin sign-in',
      'SSE 流式问答': 'SSE streaming answers',
      '测试模型连接': 'test model connection',
      '完整列表见 <a href="/docs">Swagger 文档</a>（共 34 个端点）':
        'See the <a href="/docs">Swagger docs</a> for the full list (34 endpoints)',
      '🌐 嵌入到你的网站': '🌐 Embed it in your site',
      '客服名称、头像、欢迎语不用写在这里 —— 组件会自动读取 <a href="/admin/">「客服信息」</a>的配置，改一次对所有站点生效。<br> <code>?v=1</code> 是缓存版本号，更新组件后递增它。':
        'You do not need to set the name, avatar or greeting here — the widget reads them from your <a href="/admin/">agent settings</a>, so one change applies to every site.<br> <code>?v=1</code> is a cache-busting version; bump it whenever you update the widget.',
      '更多方式（iframe / 小程序 / 桌面端）见 <a href="/widget/demo/embed.html">嵌入演示</a>':
        'For other options (iframe / mini program / desktop) see the <a href="/widget/demo/embed.html">embedding demo</a>',
      '🚀 部署到服务器': '🚀 Deploy to a server',
      '本机跑通后，把它部署到自己的服务器对外提供服务：':
        'Once it works locally, deploy it to your own server to serve real traffic:',
      '<b>最快</b>：Docker Compose 一条命令起全套（应用 + MySQL + Redis + 向量库）':
        '<b>Fastest</b>: Docker Compose brings up the whole stack in one command (app + MySQL + Redis + vector DB)',
      '<b>常规</b>：systemd 托管 + Nginx 反向代理 + HTTPS 证书':
        '<b>Conventional</b>: systemd service + Nginx reverse proxy + HTTPS certificate',
      '<b>注意</b>：SSE 流式问答需要在 Nginx 关闭缓冲，否则回答不会逐字出现':
        '<b>Heads-up</b>: SSE streaming needs buffering turned off in Nginx, otherwise answers will not appear word by word',
      '完整步骤（含 Nginx 配置、HTTPS、防火墙、备份、安全加固）见 <b>项目根目录的 <code>docs/DEPLOYMENT.md</code></b>。':
        'Full instructions (Nginx config, HTTPS, firewall, backups, hardening) live in <b><code>docs/DEPLOYMENT.md</code> at the project root</b>.'
    }
  };
  // zh-CN is the source language, so it has no catalog: t() falls through to
  // the msgid, which already IS the Simplified Chinese text.
  var L = window.CSLocale;
  var lang = L ? L.detect().locale : 'zh-CN';

  function norm(s) { return String(s).replace(/\s+/g, ' ').trim(); }
  function t(msgid) {
    var c = CAT[lang];
    return (c && c[msgid] !== undefined) ? c[msgid] : msgid;
  }
  // Snapshot the original text the first time we touch a node, so switching
  // twice (zh-CN -> en -> ja) still looks up the Chinese msgid.
  function apply() {
    if (L) L.applyHtmlLang(lang);
    var i, el, list = document.querySelectorAll('[data-i18n]');
    for (i = 0; i < list.length; i++) {
      el = list[i];
      if (el._msgid === undefined) el._msgid = norm(el.textContent);
      el.textContent = t(el._msgid);
    }
    list = document.querySelectorAll('[data-i18n-html]');
    for (i = 0; i < list.length; i++) {
      el = list[i];
      if (el._msgidHtml === undefined) el._msgidHtml = norm(el.innerHTML);
      el.innerHTML = t(el._msgidHtml);
    }
  }

  var sel = document.createElement('select');
  sel.setAttribute('aria-label', 'Language');
  sel.style.cssText = 'position:fixed;top:16px;right:16px;padding:5px 8px;' +
    'border:1px solid #e5e7eb;border-radius:6px;background:#fff;color:#222;font-size:13px';
  var locales = (L && L.LOCALES) || [
    { code: 'zh-CN', label: '简体中文' }, { code: 'zh-TW', label: '繁體中文' },
    { code: 'ja', label: '日本語' }, { code: 'en', label: 'English' }
  ];
  locales.forEach(function (o) {
    var opt = document.createElement('option');
    opt.value = o.code;
    opt.textContent = o.label;
    opt.lang = o.code;
    sel.appendChild(opt);
  });
  sel.value = lang;
  sel.addEventListener('change', function () {
    lang = sel.value;
    if (L) L.save(lang);
    apply();
  });
  document.body.appendChild(sel);

  apply();
})();
</script>
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Ensure runtime dirs exist
    Path(settings.app.upload_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.vector_db.chroma_persist_dir).mkdir(parents=True, exist_ok=True)
    Path("./data").mkdir(parents=True, exist_ok=True)

    # Auto-create tables on first boot. (Use Alembic in production.)
    # 驱动缺失/写错时，DatabaseDriverError 自带「装哪个包、连接串怎么改」的
    # 中文说明，这里不再打整段 traceback 吓人。
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as e:
        if isinstance(e, DatabaseDriverError):
            log.critical("数据库启动失败：\n%s", e)
            raise SystemExit(1)
        raise
    log.info("Database ready: %s", settings.database.url)
    log.info("Vector DB: %s / collection=%s", settings.vector_db.provider, settings.vector_db.collection)

    # Seed the admin account so a fresh install has something to log in with.
    if settings.auth.enabled:
        # Login tokens are HMAC-signed with APP_SECRET_KEY, and the documented
        # default is public knowledge — anyone can forge an admin token with
        # it. Production refuses to start on the default; other environments
        # rotate in a generated key, persisted next to the database so tokens
        # survive restarts.
        if settings.app.secret_key == security.DEFAULT_SECRET:
            try:
                settings.app.secret_key = security.resolve_secret_key(
                    settings.app.secret_key, settings.app.env
                )
            except security.DefaultSecretError as e:
                log.critical("%s", e)
                raise SystemExit(1)
            log.warning(
                "APP_SECRET_KEY 未设置——已生成随机密钥并持久化到 %s（重启不会把大家登出）。"
                "公网部署前请在 .env 里显式设置 APP_SECRET_KEY。",
                security.SECRET_FILE,
            )
        async with session_scope() as db:
            await ensure_default_admin(db)
    else:
        log.warning("AUTH_ENABLED=false — the admin console and its APIs are UNPROTECTED.")

    yield

    # Close the shared runtime singletons (LLM provider, embedder, vector
    # store) before the engine goes away, so their sockets don't get dropped
    # at interpreter exit.
    await runtime.close_shared()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app.name,
        version=settings.app.version,
        description="Document-aware multi-model customer service platform with RAG.",
        debug=settings.app.debug,
        lifespan=lifespan,
    )

    # CORS.
    #
    # "*" combined with credentials is invalid per the CORS spec — Starlette
    # then echoes any Origin back, which is equivalent to letting every site
    # read authenticated responses. The public chat endpoints need no
    # credentials, and the admin console is same-origin in normal deployments
    # (and sends a Bearer header, not a cookie, when hosted elsewhere). List
    # explicit origins in CORS_ORIGINS if cross-origin cookies are ever truly
    # needed — credentials switch on automatically then.
    origins = settings.cors.origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    install_exception_handlers(app)

    @app.middleware("http")
    async def _no_cache_frontend(request: Request, call_next):
        """Stop browsers serving a stale admin page / widget from cache.

        `no-cache` still lets a browser reuse its copy after revalidating, and
        in practice some skip the revalidation entirely — an edited widget kept
        rendering the previous build even after a hard refresh, and inside an
        iframe the parent page's refresh doesn't propagate. `no-store` removes
        the guesswork: the file is re-fetched every time.

        Cheap here (a few tens of KB, same-origin) and it eliminates a whole
        class of "I fixed it but nothing changed" confusion. If you ever serve
        the widget from a CDN, switch to a hashed filename instead.
        """
        response = await call_next(request)
        path = request.url.path
        if path.startswith(("/admin", "/widget", "/shared")) or path == "/embed":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    # ---------------------------------------------------------------------
    #  Admin page gate
    # ---------------------------------------------------------------------
    # `/admin` is a StaticFiles mount, not a route, so `Depends` can't reach it —
    # the check has to happen in middleware.
    #
    # This intentionally gates only the console *document*. Everything else under
    # /admin stays open, and two of those are load-bearing:
    #
    #   * /admin/login.html — the page you'd be redirected to. Gating it loops.
    #   * /admin/embed.html — the standalone chat window that `/embed` redirects
    #     to. Third-party sites iframe this. Gating it takes down the chat widget
    #     on every site that has embedded it.
    #
    # i18n.js and the assets are neither secret nor useful without the console.
    _GATED_ADMIN_PATHS = {"/admin", "/admin/", "/admin/index.html"}

    @app.middleware("http")
    async def _gate_admin_page(request: Request, call_next):
        settings_ = get_settings()
        if settings_.auth.enabled and request.url.path in _GATED_ADMIN_PATHS:
            # Cookie only: a browser navigating to a page cannot send an
            # Authorization header. The console's API calls use the header and
            # are checked by `require_admin` instead.
            token = request.cookies.get(settings_.auth.cookie_name)
            valid = False
            if token:
                try:
                    decode_token(token, settings_.app.secret_key)
                    valid = True
                except TokenError:
                    valid = False
            if not valid:
                # `next` is only ever used as a same-origin path by login.html.
                nxt = quote(request.url.path, safe="/")
                return RedirectResponse(f"/admin/login.html?next={nxt}", status_code=302)
        return await call_next(request)

    # Serve admin UI and widget as static files (no separate HTTP server needed).
    frontend_dir = Path(__file__).resolve().parent.parent.parent / "frontend"
    if (frontend_dir / "admin").exists():
        app.mount("/admin", StaticFiles(directory=str(frontend_dir / "admin"), html=True), name="admin")
    if (frontend_dir / "widget").exists():
        app.mount("/widget", StaticFiles(directory=str(frontend_dir / "widget"), html=True), name="widget")
    if (frontend_dir / "assets").exists():
        # Icons and donate QR codes used by the admin console.
        app.mount("/assets", StaticFiles(directory=str(frontend_dir / "assets")), name="assets")
    if (frontend_dir / "shared").exists():
        # locale.js — the offline language/region detector shared by the admin
        # console, the embed page and this landing page. The widget inlines its
        # own copy so it stays a single distributable file.
        app.mount("/shared", StaticFiles(directory=str(frontend_dir / "shared")), name="shared")

    # Routers
    #
    # Anything the admin console drives — configuration, knowledge documents,
    # model tests, the install wizard — sits behind `require_admin`, applied at
    # the router level so a newly added endpoint is protected by default rather
    # than by remembering to decorate it.
    #
    # Deliberately left open, because visitors on third-party sites call them:
    #   /api/chat                  the chat window itself
    #   /api/sessions (create + read-back of a held session id)
    #   /api/config/public         客服名称 / 头像 / 欢迎语 for the widget
    #   /api/auth/login, /state    you cannot log in from behind the login wall
    #
    # Enumerating or closing OTHER people's sessions is admin-only — it used to
    # be public, which let any anonymous visitor dump every customer chat.
    guard = [Depends(require_admin)]

    # Public subset first — a separate router because router-level dependencies
    # apply to every route in the router they're attached to.
    app.include_router(config.public_router, prefix="/api/config",   tags=["config"])
    app.include_router(auth_api.router,      prefix="/api/auth",     tags=["auth"])

    app.include_router(config.router,     prefix="/api/config",    tags=["config"],    dependencies=guard)
    app.include_router(documents.router,  prefix="/api/documents", tags=["documents"], dependencies=guard)
    app.include_router(models_api.router, prefix="/api/models",    tags=["models"],    dependencies=guard)
    app.include_router(admin_api.router,  prefix="/api/admin",     tags=["admin"],     dependencies=guard)

    app.include_router(sessions.admin_router, prefix="/api/sessions", tags=["sessions"], dependencies=guard)
    app.include_router(sessions.router,   prefix="/api/sessions",  tags=["sessions"])
    app.include_router(chat.router,       prefix="/api/chat",      tags=["chat"])

    @app.get("/health", tags=["meta"])
    async def health() -> JSONResponse:
        return JSONResponse({"status": "ok", "name": settings.app.name, "env": settings.app.env})

    @app.get("/embed", include_in_schema=False)
    async def embed_redirect(request: Request) -> RedirectResponse:
        """Standalone chat page, for embedding via ``<iframe src=".../embed">``.

        Keeps whatever query string was passed (api, title, accent, …) so the
        host page can theme the widget.
        """
        query = request.url.query
        target = "/admin/embed.html"
        return RedirectResponse(f"{target}?{query}" if query else target)

    @app.get("/", response_class=HTMLResponse, tags=["meta"])
    async def root(request: Request) -> str:
        # Use the URL the browser actually reached us on, so the embed snippet
        # is copy-pasteable on a deployed server instead of always saying
        # localhost:8000.
        origin = f"{request.url.scheme}://{request.url.netloc}"
        return f"""
        <html lang="zh-CN"><head><meta charset="utf-8"><title>{settings.app.name}</title>
        <style>body{{font-family:-apple-system,sans-serif;max-width:680px;margin:60px auto;padding:0 24px;color:#222}}
        a{{color:#0a66c2}}code{{background:#f4f4f5;padding:2px 6px;border-radius:4px;font-size:13px}}
        .card{{background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin:16px 0}}
        .btn{{display:inline-block;background:#3b82f6;color:#fff;padding:10px 20px;border-radius:6px;text-decoration:none;margin:6px 12px 6px 0;font-size:14px}}
        .btn.green{{background:#10b981}}
        .muted{{color:#6b7280;font-size:13px;line-height:1.7}}
        pre{{background:#1f2937;color:#d6deeb;padding:14px;border-radius:6px;overflow-x:auto;font-size:12.5px}}
        pre code{{background:none;color:inherit;padding:0}}
        h3{{margin-top:0}}
        ol li,ul li{{margin:6px 0}}</style></head>
        <body>
        <h1>🤖 {settings.app.name}</h1>
        <p>API: <a href="/docs">/docs</a> · <a href="/redoc">/redoc</a></p>

        <div class="card">
          <h3 data-i18n>🚀 快速开始</h3>
          <p data-i18n-html>第一次使用？打开 <a href="/admin/"><strong>管理后台</strong></a>，填入 API Key 即可。</p>
          <a class="btn" href="/admin/" data-i18n>⚙️ 进入管理后台</a>
          <a class="btn green" href="/widget/demo/" data-i18n>🪟 客服组件演示</a>
        </div>

        <div class="card">
          <h3 data-i18n>📡 API 端点</h3>
          <ul>
          <li><code>POST /api/auth/login</code> (<span data-i18n>后台登录</span>)</li>
          <li><code>GET  /api/config</code> · <code>PUT /api/config</code></li>
          <li><code>POST /api/documents/upload</code> · <code>POST /api/documents/process</code></li>
          <li><code>POST /api/chat/stream</code> (<span data-i18n>SSE 流式问答</span>)</li>
          <li><code>POST /api/models/test</code> (<span data-i18n>测试模型连接</span>)</li>
          </ul>
          <p data-i18n-html>完整列表见 <a href="/docs">Swagger 文档</a>（共 34 个端点）</p>
        </div>

        <div class="card">
          <h3 data-i18n>🌐 嵌入到你的网站</h3>
          <pre><code>&lt;script src="{origin}/widget/customer-service.js?v=1"&gt;&lt;/script&gt;
&lt;script&gt;
  CustomerService.init({{
    apiUrl: "{origin}",
    accent: "#0a66c2",
    position: "right",
  }});
&lt;/script&gt;</code></pre>
          <p class="muted" data-i18n-html>客服名称、头像、欢迎语不用写在这里 —— 组件会自动读取 <a href="/admin/">「客服信息」</a>的配置，改一次对所有站点生效。<br> <code>?v=1</code> 是缓存版本号，更新组件后递增它。</p>
          <p data-i18n-html>更多方式（iframe / 小程序 / 桌面端）见 <a href="/widget/demo/embed.html">嵌入演示</a></p>
        </div>

        <div class="card">
          <h3 data-i18n>🚀 部署到服务器</h3>
          <p data-i18n>本机跑通后，把它部署到自己的服务器对外提供服务：</p>
          <ol>
          <li data-i18n-html><b>最快</b>：Docker Compose 一条命令起全套（应用 + MySQL + Redis + 向量库）</li>
          <li data-i18n-html><b>常规</b>：systemd 托管 + Nginx 反向代理 + HTTPS 证书</li>
          <li data-i18n-html><b>注意</b>：SSE 流式问答需要在 Nginx 关闭缓冲，否则回答不会逐字出现</li>
          </ol>
          <p data-i18n-html>完整步骤（含 Nginx 配置、HTTPS、防火墙、备份、安全加固）见 <b>项目根目录的 <code>docs/DEPLOYMENT.md</code></b>。</p>
        </div>
        {_LANDING_I18N_SCRIPT}
        </body></html>
        """

    return app


app = create_app()


def main() -> None:
    """Entry point for ``python src/main.py`` (and PyCharm's ▶ button).

    Delegates to ``run.py`` in the backend root so there's exactly one
    startup implementation (banner, dependency check, CLI flags).
    """
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from run import main as run_main

    run_main()


if __name__ == "__main__":
    main()
