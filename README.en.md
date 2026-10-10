# 🤖 Intelligent Customer Service

[简体中文](README.md) · **English**

> A multi-model, RAG-powered customer-service platform built on **FastAPI**.
> **Install deps, hit Run, configure everything in the browser** — no config
> files to edit, no init scripts to run.

![status](https://img.shields.io/badge/status-working-brightgreen)
![python](https://img.shields.io/badge/python-3.11%2B-blue)
![tests](https://img.shields.io/badge/tests-208%20backend%20%2B%2032%20widget%20%2B%2095%20admin-brightgreen)
![license](https://img.shields.io/badge/license-MIT-green)

**Repositories**

| Host | URL | Notes |
| --- | --- | --- |
| GitHub | https://github.com/vfaner/intelligent-customer-service | Primary repo — file issues and PRs here |
| Gitee | https://gitee.com/super_rgh/intelligent-customer-service | Mirror for users in mainland China (faster clones); sync-only, no PRs |

---

## Contents

- [Three steps to run](#three-steps-to-run)
- [Login & account](#login--account)
- [Screenshots](#screenshots)
- [Features](#features)
- [Admin console tour](#admin-console-tour)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Supported model vendors](#supported-model-vendors)
- [Relational databases, incl. Chinese domestic (Xinchuang)](#relational-databases-incl-chinese-domestic-xinchuang)
- [Vector databases](#vector-databases)
- [Changed the embedding model? Rebuild the index in one click](#changed-the-embedding-model-rebuild-the-index-in-one-click)
- [Structure-aware chunking](#structure-aware-chunking)
- [Answer policy: strict RAG or model knowledge](#answer-policy-strict-rag-or-model-knowledge)
- [Embedding into your site](#embedding-into-your-site)
- [Languages (auto-detected offline)](#languages-auto-detected-offline)
- [API reference](#api-reference)
- [Configuration](#configuration)
- [FAQ](#faq)
- [Development](#development)
- [Deployment](#deployment)

---

## Three steps to run

### 1. Install

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Start

**PyCharm**: right-click `backend/run.py` → **Run**

**Terminal**:

```bash
python run.py
```

You'll see:

```
==============================================================
  🤖 IntelligentCustomerService
==============================================================
  ⚙️  Admin      http://localhost:8000/admin/     ← start here
  🪟  Demo       http://localhost:8000/widget/demo/
  📖  API docs   http://localhost:8000/docs
--------------------------------------------------------------
  ⚠  No chat-model API key configured yet
     → fill it in on the admin page, no file editing needed
==============================================================
```

> If a dependency is missing, the error names **your current interpreter** and
> the exact install command. `python run.py --install` installs them for you.

### 3. Configure in the browser

Open **http://localhost:8000/admin/**

You land on the login page. Sign in with the default account **`admin` /
`123456`** (see [Login & account](#login--account)), then:

| Step | What to do |
|---|---|
| 1️⃣ | If a yellow banner appears at the top, click **"🚀 One-click init"** (creates tables, dirs, default config) |
| 2️⃣ | **Model config** → pick a vendor → paste API key → **"🔌 Test connection"** → **"💾 Save"** |
| 3️⃣ | Same page, **🧬 Embedding model** → **"⬆︎ Reuse chat model's key/URL"** → **"🔌 Test & detect dimension"** |
| 4️⃣ | **Knowledge docs** → drop in txt/md/docx/xlsx/pdf → parsed and indexed automatically |
| 5️⃣ | **Chat preview** → ask a question to verify |

You never need to edit `.env` or run `init_db.py`.

<details>
<summary>CLI flags</summary>

```bash
python run.py --port 9000        # different port
python run.py --host 0.0.0.0     # allow LAN access
python run.py --no-reload        # disable hot reload
python run.py --open             # open the browser on start
python run.py --install          # auto-install missing deps
```

Standard uvicorn works too: `uvicorn src.main:app --reload --port 8000`
</details>

---

## Login & account

The console sits behind a login. Any visit to `/admin/`, `/admin` or
`/admin/index.html` without a valid session gets a **302** to
`/admin/login.html?next=…`, so you land where you were headed once you sign in.

| | |
|---|---|
| Login page | `/admin/login.html` |
| Default account | `admin` / `123456` — seeded on first startup, and only while the admin user table (`cs_admin_user`) is empty |
| Token lifetime | 7 days (`AUTH_TOKEN_TTL_HOURS=168`) |

> **Change the default password before you expose anything**, and set
> `APP_SECRET_KEY`. A banner stays in the console for as long as `123456` is
> still in use.

Two files stay reachable without a login by design: `/admin/login.html` (else
the redirect loops) and `/admin/embed.html`, which is the iframe target of
`/embed` and therefore has to load on third-party sites.

### The Account page

**👤 Account** sits in the left nav right after *Agent profile*. Two cards: your
profile (username / display name / email) and a password change (current / new /
confirm). The far right of the top bar holds a round avatar (the first letter of
your display name); hovering or clicking it opens a menu with **Profile**,
**Change password** and **Sign out** — the first two jump straight to the
matching card on this page.

- **One account by design** — no multi-user management, no roles.
- Username **3–32 characters, no spaces**. Password **at least 6 characters**.
- Changing the password **invalidates sessions on every other device
  immediately** — tokens carry a fingerprint of the password hash. The device
  that made the change is issued a fresh token and stays signed in.
- **Renaming signs nobody out.** A token resolves the account by its numeric id,
  not by username, so tokens minted before the rename keep working.
  `PUT /api/auth/me` still hands back a new token, purely so the client isn't
  left holding one with a stale username in it.

### How the credential travels

`Authorization: Bearer <token>` is the primary transport: the console can be
pointed at another origin with `?api=`, and with `CORS_ORIGINS=*` a cookie can't
be sent cross-origin at all. An HttpOnly cookie `cs_admin_token` is set as well,
but only so the server can gate the `/admin/index.html` page load before any
JavaScript runs. Tokens are signed with `APP_SECRET_KEY`, carry no server-side
session, and embed a fingerprint of the password hash — which is exactly how a
password change invalidates older tokens everywhere.

### Which endpoints need a login

| Access | Endpoints |
|---|---|
| **Admin token required** | `GET` `PUT` `/api/config` · `GET /api/config/providers` · all of `/api/documents/*` (list, parsers, upload, process, split-preview, reindex, `DELETE`) · all of `/api/models/*` (list, vector-db, available, test, test-embedding) · all of `/api/admin/*` (status, init, version) · `GET` `PUT` `/api/auth/me` · `PUT /api/auth/me/password` |
| **Open, no login** | `GET /health` · `GET /api/config/public` · `GET /api/auth/state` · `POST /api/auth/login` · `POST /api/auth/logout` · all of `/api/sessions/*` · `POST /api/chat` · `POST /api/chat/stream` |

The second group has to stay open: the embedded widget calls it from anonymous
visitors' browsers, and so do the console's own *Chat preview* and *Embed guide*
pages.

That is why **`GET /api/config/public`** exists. It returns only `name`,
`avatar`, `welcome_message`, `contact_phone` and `contact_email`, and the widget
reads that now. Plain `GET /api/config` additionally carries API base URLs,
model names and vector-DB coordinates — which is why it is behind a login.

---

## Screenshots

> 📺 **Video demo (Bilibili)**: [Full walkthrough of the system · BV1zRYM6VEDJ](https://www.bilibili.com/video/BV1zRYM6VEDJ)

**Sign-in required** — every page under `/admin/**` is gated, with a ZH / ZH-TW / JA / EN switcher:

<table>
<tr>
<td width="50%"><b>🔐 Sign-in page</b><br>Default credentials <code>admin / 123456</code>. No pages are accessible without logging in.</td>
<td width="50%"><b>👤 Account info</b><br>Change username, display name, email, and password.</td>
</tr>
<tr>
<td><img src="frontend/assets/ai_kefu_login.jpg" alt="Admin console sign-in page"></td>
<td><img src="frontend/assets/ai_kefu_admin.png" alt="Account info"></td>
</tr>
<tr>
<td><b>📊 Overview</b><br>Current model, vector store and document count at a glance, plus a four-step quick start.</td>
<td><b>🤖 Model config</b><br>15 vendor presets; picking one fills in the base URL and model. Connection test and dimension detection included.</td>
</tr>
<tr>
<td><img src="frontend/assets/ai_kefu_gailan.png" alt="System overview"></td>
<td><img src="frontend/assets/ai_kefu_model.png" alt="Model configuration"></td>
</tr>
<tr>
<td><b>🔍 RAG settings</b><br>Answer-policy switch, retrieval parameters, chunking strategy — with a live chunking preview.</td>
<td><b>🧠 Vector store</b><br>Chroma / Qdrant / Milvus, with only the relevant fields shown.</td>
</tr>
<tr>
<td><img src="frontend/assets/ai_kefu_rag.png" alt="RAG settings"></td>
<td><img src="frontend/assets/ai_kefu_xl.png" alt="Vector store configuration"></td>
</tr>
<tr>
<td><b>📄 Knowledge base</b><br>Drag in txt / md / docx / xlsx / pdf — parsed, chunked, embedded and indexed automatically.</td>
<td><b>💬 Agent profile</b><br>Name, avatar, greeting, contact details — set once, applies to every embedded site.</td>
</tr>
<tr>
<td><img src="frontend/assets/ai_kefu_doc.png" alt="Knowledge base"></td>
<td><img src="frontend/assets/ai_kefu_config.png" alt="Agent profile"></td>
</tr>
<tr>
<td><b>🪟 Chat preview</b><br>Test RAG answers inside the console. Markdown is rendered and links are clickable.</td>
<td><b>🌐 Embed guide</b><br>script / iframe / mini-program snippets, one click to copy.</td>
</tr>
<tr>
<td><img src="frontend/assets/ai_kefu_view.png" alt="Chat preview"></td>
<td><img src="frontend/assets/ai_kefu_jieru.png" alt="Embed guide"></td>
</tr>
</table>

**What visitors see** — a floating button in the corner that opens the chat window:

<p align="center">
  <img src="frontend/assets/ai_kefu_yanshi.png" alt="Embedded widget in action" width="720">
</p>

---

## Features

| Area | What you get |
|---|---|
| **📄 Knowledge base** | `txt` / `md` / `docx` / `xlsx` / `pdf` → structure-aware chunking → embedding → indexed. MD5 dedup, **resumable ingestion** |
| **🧠 Smart chunking** | Recognises `【sections】`, Markdown headings, `第X章`, Q&A pairs, Excel sheets; splits on semantic boundaries and keeps heading context |
| **🤖 Multi-model** | 15 vendor presets: DeepSeek, Qwen/Bailian, Volcengine ARK (subscription and metered kept separate), Zhipu, Kimi, Qianfan, OpenAI, Claude, Gemini, Ollama, Shengsuanyun, Youyunjisuan, plus 2 custom slots |
| **🔁 Dual protocol** | OpenAI `/chat/completions` and Anthropic `/messages`; switching protocol updates the base URL |
| **🗂 Three vector stores** | **Chroma** (default, local files) / **Qdrant** / **Milvus** (incl. zero-install Lite) |
| **♻️ One-click rebuild after a model swap** | Confirm once after changing the embedding model and the old collection is dropped and everything re-embedded in the background: visible progress, automatic rate-limit backoff, resumable checkpoints. Detects a stale dimension lock even on an empty collection (warns with 0 documents) |
| **🎯 Answer policy** | Strict RAG by default (refuse anything outside your docs); optional switch to let the model answer from its own knowledge |
| **⚡ Streaming** | SSE token streaming, with automatic 429 retry so a rate limit doesn't kill the reply |
| **🪟 Embeddable widget** | Single-file JS, zero dependencies, no build step. Renders Markdown and makes links clickable |
| **📲 Cross-platform** | Websites / WeChat mini-program `web-view` / Electron / Tauri / iOS / Android |
| **⚙️ Browser-only config** | Models, vector store, RAG params, agent profile — all editable in the UI, effective immediately without a restart |
| **🔐 Admin login** | The console and every config/document/model endpoint sit behind a login. Default `admin` / `123456`, 7-day tokens, single account, password change signs out other devices |
| **🌐 Four languages, detected offline** | Simplified / Traditional Chinese, Japanese, English. **Region comes from the browser's time zone — no IP lookup, so it still works on an air-gapped intranet.** Switch by hand any time; the AI answers in whatever language is active |

---

## Admin console tour

Nine menu items on the left of `/admin/`:

| Menu | Purpose |
|---|---|
| 📊 **Overview** | System status, current model, vector store, document count |
| 🤖 **Model config** | Chat model (vendor/protocol/key/model/temperature) + embedding model, with connection test, **automatic dimension detection**, and **one-click vector-index rebuild** after a model swap |
| 🧠 **Vector store** | Switch between Chroma / Qdrant / Milvus |
| 🔍 **RAG settings** | **Answer-policy switch** + top-K/threshold + chunking, with **chunking preview** |
| 📄 **Knowledge docs** | Drag-and-drop upload, **live ingestion progress**, resume on failure, delete |
| 💬 **Agent profile** | Name, avatar, greeting, contacts (the widget reads these automatically) |
| 👤 **Account** | Username / display name / email, and change your password — see [Login & account](#login--account) |
| 🪟 **Chat preview** | The real widget in an iframe — test RAG answers here |
| 🌐 **Embed guide** | Three embedding methods, copy-paste ready |

Five persistent tools sit in the top-right corner:

| Tool | Behaviour |
|---|---|
| 🟢 **Backend status** | Polls every 15s. Hover to see the full error when offline |
| 🌐 **Language** | Switches the console between 简体中文 / 繁體中文 / 日本語 / English. Each option is labelled in its own script, so a visitor who can't read the current language can still find theirs |
| ⬆︎ **Check for updates** | Compares the running version with the latest GitHub release; a red dot appears when one is available |
| ☕ **Donate** | WeChat / Alipay / QQ QR codes |
| 👤 **Signed-in account** | Round avatar at the far right; hover or click for Profile / Change password / Sign out |

> **The update check needs a repo**: set `APP_GITHUB_REPO=owner/repo` in
> `backend/.env`. Without it the button explains how to configure it rather
> than failing.
>
> It **only checks — it never touches your code**. When a newer release exists
> it shows the release notes and the update commands, and you decide when to
> run them. Auto-`git pull` would overwrite uncommitted local changes and can
> leave the service unable to start, so it isn't wired to a button.

### Desktop layout and scrollbars

At widths ≥ 721px the **sidebar and top bar stay put; only the content area
scrolls**. `body` has page scrolling switched off, `.main` carries its own
`overflow-y: auto`, and the top bar is pinned with `position: sticky`.

The scrollbar is drawn by hand rather than left to the system:

| Trait | How |
|---|---|
| Thin and rounded | A 4px pill squeezed out of a 10px gutter with `3px solid transparent` + `background-clip: content-box` — it looks thin but the hit area is still 10px |
| Oval caps | `border-radius: 999px` |
| Hidden at rest | `background-color: transparent` by default; fades in while scrolling (a script adds `.is-scrolling`) or while the pointer is over the content area, then fades out about a second after scrolling stops |
| Dark sidebar | Uses translucent white instead — grey is invisible on the dark panel |

Two traps worth recording, both hit during development:

- **`scrollbar-width` silently kills `::-webkit-scrollbar`.** If either
  `scrollbar-width` or `scrollbar-color` is set to anything other than `auto`,
  Chrome ignores the `::-webkit-scrollbar` pseudo-elements *entirely* and falls
  back to the native scrollbar (a zero-width overlay on macOS) — the pill never
  renders. The two systems are mutually exclusive, so the standard properties
  live inside `@supports not selector(::-webkit-scrollbar)`, which is true in
  Chrome/Safari and false in Firefox — exactly the split needed.
- **Sticky offsets constrain the margin box, not the border box.** The top bar
  uses `margin-top: -32px` to bleed past `.main`'s padding; with `top: 0` the
  border box lands at y=32, leaving a 32px unpainted strip that scrolled content
  shows through. It needs `top: -32px`.

Below 721px none of this applies: the sidebar becomes a drawer and the page
scrolls normally.

---

## Architecture

```
┌──────────── Browser ────────────┐        ┌─────────── FastAPI backend ────────┐
│  customer-service.js (0 deps)   │  HTTP  │  api · services · adapters         │
│   float button + chat + upload  │ ◀────▶ │  vector_store · embeddings         │
│   SSE streaming + Markdown      │  SSE   │  parsers · prompts · models        │
└─────────────────────────────────┘        └──┬────────┬──────────┬────────────┘
                                              │        │          │
                                          SQLite    Chroma /   LLM vendors
                                          /MySQL    Qdrant /   (15 presets)
                                          (sessions) Milvus
```

**LLM adapters** (async + SSE, automatic 429 retry):

```
BaseProvider
  ├── OpenAIStyleProvider     → POST {base}/chat/completions
  └── AnthropicStyleProvider  → POST {base}/messages
                                (system is a top-level field; sends both
                                 x-api-key and Authorization: Bearer so
                                 vendor gateways all work)
build_provider() dispatches on protocol
```

---

## Project layout

```
intelligent-customer-service/
├── backend/
│   ├── run.py                        ← one-click start (PyCharm: right-click → Run)
│   ├── requirements.txt
│   ├── requirements-xinchuang.txt    Dameng DM8 drivers (optional, Linux/Windows only)
│   ├── .env.example                  (optional; UI config takes precedence)
│   ├── docker-compose.yml            MySQL + Qdrant + Milvus
│   ├── src/
│   │   ├── main.py                   FastAPI app factory
│   │   ├── api/                      admin · auth · chat · config · documents · models · sessions
│   │   ├── services/                 document · rag · llm · embedding · session
│   │   ├── adapters/                 base · openai · anthropic · factory
│   │   ├── vector_store/             base · chroma · qdrant · milvus · factory
│   │   ├── embeddings/               base · openai_compatible_embedder
│   │   ├── parsers/                  txt (incl. md) · docx · xlsx · pdf
│   │   ├── prompts/system_prompts.py RAG constraints + fallback prompts
│   │   ├── models/                   SQLAlchemy ORM
│   │   ├── core/                     config · database · registry · exceptions
│   │   ├── utils/                    text_splitter (structure-aware) · sse · hash · obfuscation
│   │   └── tests/                    208 tests (73 auth, 20 DB-driver detection, 10 rebuild state machine)
│   ├── scripts/
│   │   ├── init_db.py                create tables (one-click init covers this)
│   │   ├── check_env.py              dependency/config self-check
│   │   ├── reset_admin_password.py   ← reset the admin password from the shell
│   │   ├── ingest_docs.py            bulk import
│   │   └── ingest_retry.py           ← auto-retry ingestion for large corpora
│   └── samples/                      sample knowledge doc
├── frontend/
│   ├── shared/
│   │   └── locale.js                 ← offline region detection (time zone first, language second)
│   ├── admin/
│   │   ├── index.html                admin console
│   │   ├── login.html                ← sign-in page (reachable without a login)
│   │   ├── embed.html                standalone chat page (preview / iframe)
│   │   ├── i18n.js                   ← catalogs for 4 languages (Simplified Chinese is the source, so it needs none)
│   │   ├── i18n-check.mjs            ← catalog coverage check (fails on any missing translation)
│   │   ├── mark-i18n.py              ← adds data-i18n markers to the HTML (re-runnable)
│   │   ├── selftest.mjs              ← console self-test: language switching + account menu + rebuild logic (58 assertions)
│   │   └── login-selftest.mjs        ← sign-in page self-test (37 assertions)
│   ├── assets/                       icons, donate QR codes, screenshots
│   └── widget/
│       ├── customer-service.js       embeddable widget (zero deps)
│       ├── selftest.mjs              ← 32 assertions in a real DOM
│       └── demo/                     embedding demos
└── docs/
    ├── API.md · DEPLOYMENT.md · EMBED_GUIDE.md
    └── qqmu-knowledge-base/          sample corpus (regenerable via scripts)
```

---

## Supported model vendors

The dropdown groups vendors and custom slots. Picking a vendor fills in its
base URL and default model; click 🔄 to fetch the models **your account can
actually use**.

| Vendor | Protocol | Notes |
|---|---|---|
| DeepSeek | openai | Good price/performance |
| Alibaba Cloud · Qwen / Bailian | openai | Shares the DashScope compatible endpoint |
| Volcengine ARK · **subscription** | openai + anthropic | `/api/plan/v3`, `/api/plan/v1`; model must be `ark-code-latest` |
| Volcengine ARK · **Doubao** (metered) | openai | `/api/v3` |
| Zhipu GLM | openai | |
| Kimi (Moonshot) | openai | |
| Baidu Qianfan (ERNIE) | openai | v2 endpoint |
| OpenAI | openai | |
| Anthropic Claude | anthropic | Native Messages protocol |
| Shengsuanyun (router) | openai | Aggregates several vendors |
| Youyunjisuan ⚠ | openai | |
| Google Gemini ⚠ | openai | Via the OpenAI-compatible endpoint |
| Ollama (local) ⚠ | openai | `localhost:11434`, any key works |
| Custom (OpenAI protocol) | openai | vLLM / one-api / LiteLLM / your own gateway |
| Custom (Anthropic protocol) | anthropic | Any Anthropic-compatible layer |

> **⚠ marks presets I could not verify against the vendor**; the UI shows an
> orange warning for them. Check the vendor's docs, or click 🔄 to fetch the
> real model list. Everything else has been tested against a live account.

### ⚠️ Volcengine ARK users

ARK has **two products with different billing**. Picking the wrong one costs
money:

| Your situation | Choose | Base URL |
|---|---|---|
| You bought a **Coding Plan subscription** | ARK · subscription | `/api/plan/v3` (OpenAI) or `/api/plan/v1` (Anthropic) |
| **Pay-as-you-go** | ARK · Doubao | `/api/v3` |

For the subscription the model name must be `ark-code-latest`; which
underlying model it routes to is chosen in the ARK console.

---

## Relational databases, incl. Chinese domestic (Xinchuang)

Sessions, console settings and document metadata live in a relational database.
SQLite is the zero-config default; for production or a 信创 (Xinchuang) stack,
just change `DATABASE_URL` in `.env` — tables are created automatically on
startup. The ORM uses only portable column types (JSON, DateTime, …), nothing
dialect-specific. The engine is fully asynchronous, so the URL **must use an
async driver**; if a driver is missing (or a synchronous one was typed), the
startup error names the exact package to install instead of SQLAlchemy's bare
`Can't load plugin`.

| Database | URL scheme | Driver |
|---|---|---|
| **SQLite** (default) | `sqlite+aiosqlite` | bundled in requirements.txt |
| **MySQL / MariaDB** | `mysql+aiomysql` | bundled |
| **PostgreSQL** | `postgresql+asyncpg` | bundled (`asyncpg` added) |
| 🇨🇳 **Dameng DM8** | `dm+dmAsync` (default port 5236) | `pip install -r requirements-xinchuang.txt` |
| 🇨🇳 **KingbaseES** (人大金仓) | `postgresql+asyncpg` (PG-compatibility mode, port 54321) | bundled |
| 🇨🇳 **openGauss** | `postgresql+asyncpg` | bundled |
| 🇨🇳 **HighGo / Vastbase G100 / GaussDB (PG edition)** | `postgresql+asyncpg` | bundled |
| 🇨🇳 **TiDB / OceanBase (MySQL tenant) / GaussDB(for MySQL)** | `mysql+aiomysql` | bundled |

**Why standard drivers work with domestic databases**: KingbaseES, openGauss,
HighGo, Vastbase and GaussDB speak the PostgreSQL wire protocol; TiDB and
OceanBase speak MySQL's. Pointing the same driver at the domestic database's
host and port is usually enough.

**Two caveats**:

1. **openGauss's default sha256 authentication** is only supported by its own
   synchronous psycopg2 fork — asyncpg can't negotiate it. Switch the server to
   md5/scram auth. KingbaseES must be initialised in **PostgreSQL-compatibility
   mode** (the vendor `kingbase8` dialect is synchronous and unusable with this
   project's async engine).
2. **Dameng DM8** uses the official trio `dmPython` + `dmSQLAlchemy` +
   `dmAsync` (the `dm+dmAsync` dialect is asynchronous, published by Dameng).
   Dameng only publishes compiled wheels for Linux (x86_64/aarch64 — Kylin and
   UOS included) and Windows; **there is no macOS wheel**, so the trio lives in
   the separate [requirements-xinchuang.txt](backend/requirements-xinchuang.txt).
   `pip install -r` it on the 信创 server; developer laptops keep installing
   the main requirements.

See [backend/.env.example](backend/.env.example) for URL examples, and
[db_drivers.py](backend/src/core/db_drivers.py) for the missing-driver error
mapping. Run `python scripts/check_env.py` to preflight a database switch.

---

## Vector databases

Switch under **Vector store**. After switching you must **restart the backend
and re-ingest your documents** — vector data doesn't migrate between stores.
(Merely changing the *embedding model* needs no manual folder deletion — use
the one-click rebuild described in the next section.)

| Store | Notes | Extra service? |
|---|---|---|
| **Chroma** (default) | Local files under `./data/chroma_db` | ❌ works out of the box |
| **Qdrant** | Rust, production-grade | ✅ `docker compose up -d qdrant` |
| **Milvus** | Enterprise-grade. A `.db` path uses **Milvus Lite** (zero install); `http://host:19530` connects to a server; Zilliz Cloud also works | Depends on mode |

---

## Changed the embedding model? Rebuild the index in one click

A vector collection is permanently bound to **the embedding model that created it**. After a model swap the old vectors are unusable:

- **Different dimensions** (e.g. a 2048-d multimodal model → a 1024-d `text-embedding`): new vectors are rejected outright with `Collection expecting embedding with dimension of 2048, got 1024`. **Deleting every document doesn't help** — even an empty collection keeps its dimension lock;
- **Same dimension, different model**: no error, but the two embedding spaces are incompatible and retrieval silently turns into noise.

The only correct response is to **drop the collection and re-embed every document**. That's a single guided action in the UI:

1. On the **Model config** page, update the embedding model and click **🔌 Test & detect dimension** (saving first also works)
2. If the test succeeds and an incompatibility is found, a confirmation dialog appears: old → new model, both dimensions, number of affected documents, and the warning that API quota will be consumed. Nothing starts until you confirm
3. The job runs in the background; a banner on the Model config page and a slim notice on the Knowledge docs page show progress (`documents i/N · current file`, polled every 3 s and paused when the page isn't visible). Per-chunk progress stays in the documents table's own poller

Pipeline: **probe embedding** (bad key/endpoint fails before any data is touched) → **drop & recreate the collection** at the probed dimension → reset every document row → re-parse/chunk/embed each document.

| Design point | Behaviour |
|---|---|
| **Confirm, never fully automatic** | People click "test connection" just to try a model; full automation could burn quota re-embedding a 9 000-chunk knowledge base before the user has decided |
| **Automatic rate-limit backoff** | 429 / rate limit / timeout wait and retry (from 60 s, up to 4×). Deterministic failures (corrupt file, bad key) fail fast and list the offending filenames |
| **Resumable checkpoints** | Embedding runs in batches of 50 chunks. After an interruption or restart, "Retry" only processes unfinished documents/chunks — completed work is never re-embedded or re-billed |
| **Model signature** | The config records the build-time `{provider, model, base_url, dim}`. **Rotating the API key alone never triggers a rebuild**; with no documents and no dimension conflict nothing nags you |
| **Empty-collection lock detection** | The stored collection's locked dimension is read directly, so the confusing case — "all documents failed/were deleted, the collection is empty, yet inserts still fail with a dimension error" — is flagged as soon as you open the page |
| **Endpoints** | `POST /api/documents/reindex` (409 while a job is running), `GET /api/documents/reindex/status`. Both require an admin login |

---

## Structure-aware chunking

The default strategy. Instead of cutting every N characters, it splits on
semantic boundaries and keeps the heading context.

Measured on `samples/sample_knowledge.txt` (595 chars):

| | Fixed window | Structure-aware |
|---|---|---|
| Chunks | 2 | **6** |
| Problem | Four sections crammed together; another chunk started mid-`Q3`, losing the `【FAQ】` context | Each section its own chunk, heading intact |
| Top-1 score for "warranty period" | 0.719 | **0.791** |

**Recognised structures**: `【section】` · Markdown `#`–`######` (with nesting)
· `第X章` · `Q:/A:` pairs · `## Sheet:` (Excel) · `## Page N` (PDF)

Numbered list items like `1. Warranty is 12 months.` are **not** mistaken for
headings.

**Tune it in the UI** under *RAG settings → Chunking*:

- **Strategy** — structure-aware / fixed window
- **Heading prefix** — long sections carry a `[Specs]` prefix on each chunk
- **🔍 Preview chunking** — pick a document or paste text and see the chunk
  count, size distribution and section attribution **without ingesting**

---

## Answer policy: strict RAG or model knowledge

A switch under *RAG settings → Answer policy*, **off** by default.

| | Off (default) | On |
|---|---|---|
| Knowledge base has the answer | Answers from your docs | Answers from your docs, fills gaps if needed |
| Knowledge base **doesn't** | "Sorry, I don't have that information" | Answers from the model's own knowledge |
| Traceability | ✅ Every sentence traces to your docs | ❌ Users can't tell which parts came from your docs |
| Good for | Pricing, policies, commitments | General Q&A, tech support |

Measured (asking for the height of Mount Everest):

```
Off: 🚫 Sorry, I don't have that information in the knowledge base.
On:  ✅ Mount Everest's official height is 8,848.86 m, announced jointly by
        China and Nepal in December 2020…
```

**On-topic questions are unaffected** when the switch is on — "do you have 404
page templates?" still answers from the knowledge base.

<details>
<summary>Why <code>relevance_threshold</code> (0.76) exists</summary>

With CJK embeddings, **completely unrelated text still scores around 0.65**.
Meanwhile `similarity_threshold` has to stay low (0.5) or short queries like
"login page" retrieve nothing at all.

Measured score distribution on this corpus:

```
On-topic:  0.813 0.827 0.833 0.836 0.850 0.859 0.905
Off-topic: 0.649 0.665 0.680 0.702 0.711
Gap: +0.102  → threshold set at 0.76
```

So `relevance_threshold` separates "we retrieved something" from "we retrieved
an answer". Other corpora may need this tuned.
</details>

---

## Embedding into your site

### Option 1: `<script>` tag (simplest)

```html
<script src="http://localhost:8000/widget/customer-service.js?v=1"></script>
<script>
  CustomerService.init({
    apiUrl: 'http://localhost:8000',
    accent: '#0a66c2',
    position: 'right',      // 'left' | 'right'
    // Off by default: the document endpoints require an admin login
    enableUpload: false,
  });
</script>
```

> Agent name, avatar and greeting **don't belong here** — the widget reads them
> from the *Agent profile* page, so one edit applies everywhere.
> `?v=1` is a cache-buster; bump it after updating the widget.

### Option 2: iframe (full isolation)

```html
<iframe src="http://localhost:8000/embed?api=http://localhost:8000"
        style="position:fixed;right:20px;bottom:20px;width:400px;height:636px;
               border:0;background:transparent;">
</iframe>
```

> **Size it properly and keep it transparent.** The expanded panel needs
> `400 × 636` (panel 360×540 + button clearance 76 + margin 20). Smaller clips
> the panel; without `background: transparent` the unused area shows the
> iframe's own colour as a visible block.

`/embed` accepts `api` · `title` · `accent` · `position` · `autoOpen` ·
`upload` · `bg`.

### Option 3: mini-programs

```xml
<web-view src="https://your-domain/embed?api=https://your-api-domain" />
```

Remember to whitelist the domain in the mini-program console.

### Widget options

| Option | Default | Meaning |
|---|---|---|
| `apiUrl` | `http://localhost:8000` | Backend base URL |
| `title` / `subtitle` | from backend | Header text (passing it overrides the backend) |
| `avatar` | from backend | Image URL; falls back to the first character on error |
| `welcome` | from backend | First assistant message |
| `accent` | `#0a66c2` | Theme colour |
| `position` | `right` | Corner for the floating button |
| `autoOpen` | `false` | Open on load |
| `enableUpload` | `false` | Show the upload button (see note below) |
| `useServerConfig` | `true` | Pull agent profile from `/api/config/public` |
| `sessionId` | `null` | Resume a previous session |
| `lang` | `auto` | `auto` / `zh-CN` / `zh-TW` / `ja` / `en` — see [Languages](#languages-auto-detected-offline) |
| `onReady` | `null` | Callback after init |
| `onLangChange` | `null` | Callback after the visitor switches language |

```js
CustomerService.open() / close() / toggle() / sendMessage(text) / destroy()
CustomerService.setLang('ja') / getLang()
```

> **`enableUpload` defaults to `false`** (it used to be `true`).
> `/api/documents/upload` requires an admin token, so a visitor pressing the
> button would only get a `401`. Turn it on only where the person using the
> widget is signed in to the console.

See [docs/EMBED_GUIDE.md](docs/EMBED_GUIDE.md) for Electron / Tauri / iOS /
Android / CSP details.

---

## Languages (auto-detected offline)

Both the interface and the AI's answers support **简体中文 / 繁體中文 / 日本語 /
English**. All four surfaces can switch: the admin console, the chat widget, the
standalone `/embed` page, and the backend landing page.

**How the default is chosen**: the browser's time zone decides the region; if the
time zone is inconclusive, `navigator.languages` decides; with no signal at all,
English.

| Time zone | Default language |
|---|---|
| `Asia/Shanghai`, `Asia/Urumqi`, `Asia/Chongqing` | 简体中文 |
| `Asia/Taipei`, `Asia/Hong_Kong`, `Asia/Macau` | 繁體中文 |
| `Asia/Tokyo` | 日本語 |
| anything else | English |

**Why not IP geolocation**: on an intranet the client address is `10.x` or
`192.168.x`, which carries no region information at all, and an offline
deployment can't reach any online IP database either. Time zone and language
preferences both come from the browser itself, so **detection works exactly the
same with no internet and no route out of the LAN**.

**Switching by hand**: the 🌐 button at the top right of the admin console and
the chat window's header. The choice is stored in `localStorage` under
`cs_lang` — one key shared by the console and the widget, so changing it in one
place moves the other.

**The language of the answers**: the active language is sent with every
question. The knowledge base is still Chinese and not one line of retrieval
logic changed — the tuned Chinese prompt is reused verbatim and an
"answer in this language" directive is appended to it, with that directive
itself *written in the target language* (a Japanese instruction written in
Japanese holds up far better). For `zh-CN` the appended string is empty, so the
prompt is **byte-identical** to the pre-i18n one.

To force a language: pass `lang: 'ja'` to the widget, add `?lang=ja` to `/embed`
or the console, or send `{"lang":"ja"}` to the API.

```bash
# All checks must be green: 208 backend, 32 widget, 58 admin + 37 login, plus catalog coverage
cd backend && .venv/bin/pytest src/tests -q
cd frontend/widget && npm i && node selftest.mjs
cd frontend/admin && node selftest.mjs && node login-selftest.mjs && node i18n-check.mjs
```

Changed some Chinese text in the console? `node i18n-check.mjs` tells you which
entries lack a translation. A catalog key **is** the Chinese source text
(`frontend/admin/i18n.js`), so Simplified Chinese needs no catalog, and a
missing translation falls back to Chinese instead of showing a raw key. To add
`data-i18n` markers to new HTML, run `python3 mark-i18n.py` (safe to re-run).

---

## API reference

Swagger: **http://localhost:8000/docs** · Details: [docs/API.md](docs/API.md)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check |
| `GET` | `/api/admin/status` | Install status (drives the setup banner) |
| `POST` | `/api/admin/init` | One-click init (idempotent) |
| `GET` | `/api/admin/version` | Update check against the latest GitHub release |
| `GET` | `/api/auth/state` | Whether auth is required and whether the default password is still in use (public) — `{"auth_required": true, "default_password": true}` |
| `POST` | `/api/auth/login` | `{username, password}` → `{access_token, token_type, expires_in, user}` |
| `POST` | `/api/auth/logout` | Clears the cookie |
| `GET` | `/api/auth/me` | Current account |
| `PUT` | `/api/auth/me` | Update `username` / `display_name` / `email`; returns a fresh token |
| `PUT` | `/api/auth/me/password` | `{old_password, new_password}`; returns a fresh token |
| `GET` | `/api/auth/ping` | Cheap "is my token still valid" check |
| `GET` `PUT` | `/api/config` | Read / update all config |
| `GET` | `/api/config/public` | Agent profile only (`name`, `avatar`, `welcome_message`, `contact_phone`, `contact_email`) — what the widget reads |
| `GET` | `/api/config/providers` | Vendor registry |
| `POST` | `/api/documents/upload` | Upload a document (multipart) |
| `POST` | `/api/documents/process` | Chunk + embed + index (resumable) |
| `POST` | `/api/documents/split-preview` | Preview chunking without ingesting |
| `GET` | `/api/documents/list` | Documents with ingestion progress |
| `DELETE` | `/api/documents/{id}` | Delete a document and its vectors |
| `GET` | `/api/documents/parsers` | Supported file types |
| `POST` | `/api/documents/reindex` | One-click index rebuild after an embedding-model swap (background job; 409 while running) |
| `GET` | `/api/documents/reindex/status` | Rebuild progress + model-signature/dimension-lock mismatch |
| `POST` | `/api/chat` | One-shot answer (JSON) |
| `POST` | `/api/chat/stream` | **Streaming answer (SSE)** |
| `GET` `POST` | `/api/sessions` | List / create sessions |
| `GET` | `/api/sessions/{id}/messages` | Session history |
| `POST` | `/api/sessions/{id}/close` | Close a session |
| `GET` | `/api/models` | Models + vector stores overview |
| `POST` | `/api/models/available` | Fetch the vendor's real model list |
| `POST` | `/api/models/test` | Test the chat model |
| `POST` | `/api/models/test-embedding` | Test embedding and detect dimension |
| `GET` | `/api/models/vector-db` | Vector store list |

> Most of these need an admin token — see
> [Which endpoints need a login](#which-endpoints-need-a-login) for the exact
> split.

**Static pages** (served by the backend — no separate web server needed):

| Path | What |
|---|---|
| `/` | Landing page with shortcuts |
| `/admin/` | **Admin console** (redirects to the login page without a session) |
| `/admin/login.html` | Sign-in page |
| `/embed` | Standalone chat page for iframes (query string preserved) |
| `/widget/customer-service.js` | The widget |
| `/widget/demo/` | Embedding demos |
| `/assets/` | Icons, QR codes, screenshots |
| `/docs` · `/redoc` | Swagger / ReDoc |

**SSE events** on `/api/chat/stream`:

```
event: meta      → first, carries {session_id}
event: token     → incremental text {text}
event: sources   → citations (heading / score / filename)
event: done      → {ok}
event: error     → {message}
```

---

## Configuration

**UI config takes precedence over `.env`** — you never need to touch `.env` in
normal use. This table is for deployment scripts and CI.

<details>
<summary>All .env variables</summary>

> ⚠️ Variables in the App section **need the `APP_` prefix** (pydantic-settings
> `env_prefix`). The template once used bare `DEBUG` / `PORT` / `SECRET_KEY`,
> which were silently ignored — a production deploy stayed in debug mode.

| Group | Variable | Default |
|---|---|---|
| App | `APP_ENV` / `APP_DEBUG` / `APP_HOST` / `APP_PORT` | `development` / `true` / `0.0.0.0` / `8000` |
| | `APP_SECRET_KEY` | `change-me-in-production` (**signs login tokens — change it**) |
| | `UPLOAD_DIR` / `MAX_UPLOAD_MB` | `./uploads` / `20` |
| | `APP_GITHUB_REPO` / `APP_VERSION` | `vfaner/intelligent-customer-service` / read from `pyproject.toml` (update check; you normally shouldn't set `APP_VERSION`) |
| Auth | `AUTH_ENABLED` | `true` (`false` disables every guard) |
| | `AUTH_TOKEN_TTL_HOURS` | `168` (7 days) |
| | `AUTH_COOKIE_NAME` | `cs_admin_token` |
| | `AUTH_DEFAULT_USERNAME` / `AUTH_DEFAULT_PASSWORD` | `admin` / `123456` (seed account) |
| Database | `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db` |
| Vector store | `VECTOR_DB_PROVIDER` | `chroma` (`chroma`\|`qdrant`\|`milvus`) |
| | `VECTOR_DB_HOST` / `_PORT` / `_COLLECTION` | `localhost` / `6333` / `knowledge_base` |
| | `VECTOR_DB_EMBEDDING_DIM` | `1024` |
| | `CHROMA_PERSIST_DIR` | `./data/chroma_db` |
| | `VECTOR_DB_MILVUS_URI` / `_TOKEN` / `_DB_NAME` | `./data/milvus.db` / empty / empty |
| LLM | `LLM_PROVIDER` / `_API_KEY` / `_BASE_URL` / `_MODEL` | `deepseek` / empty / … / `deepseek-chat` |
| | `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `API_PROTOCOL` | `0.3` / `2048` / `openai` |
| Embedding | `EMBEDDING_API_KEY` / `_BASE_URL` / `_MODEL` / `_DIM` | empty / … / `text-embedding-3-small` / `1024` |
| RAG | `RAG_TOP_K` / `RAG_SIMILARITY_THRESHOLD` | `5` / `0.5` |
| | `RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP` | `500` / `100` |
| | `RAG_CHUNK_STRATEGY` / `RAG_CHUNK_PREFIX_HEADING` | `structure` / `true` |
| | `RAG_ALLOW_MODEL_KNOWLEDGE` / `RAG_RELEVANCE_THRESHOLD` | `false` / `0.76` |
| | `RAG_MAX_CONTEXT_CHARS` | `8000` |
| CORS | `CORS_ORIGINS` | `*` |

</details>

> ⚠️ **`APP_SECRET_KEY` now signs the login tokens** and **must be changed
> before deployment** — the backend logs a warning at startup if it is still the
> default outside development.
>
> `AUTH_ENABLED=false` switches off every guard: no login page, no token checks.
> Only for a throwaway demo on a private network.

> ⚠️ **`backend/data/app.db` holds the API keys you enter in the UI**
> (base64-obfuscated, effectively plaintext). It's excluded by `.gitignore` —
> **never commit it to a public repo.**

---

## FAQ

<details>
<summary><b>"Missing dependency" but I already installed it</b></summary>

You likely installed into a different Python environment. The error prints
**your current interpreter's** full path — use that:

```bash
/your/venv/bin/python -m pip install -r requirements.txt
# or let the script handle it
python run.py --install
```

PyCharm users: check the interpreter in your Run Configuration matches.
</details>

<details>
<summary><b>I forgot the admin password</b></summary>

Reset it from the shell, in the `backend/` directory:

```bash
cd backend
python -m scripts.reset_admin_password
```

It prompts for the new password without echoing it. `--list` shows which
accounts exist, and `--username X --password Y` is the non-interactive form.

Resetting **signs out all devices**.
</details>

<details>
<summary><b>Can I run it without an API key?</b></summary>

Yes. The service starts and everything that isn't an LLM call works. Endpoints
that need a model return a clear `llm_error` / `embedding_error`.
</details>

<details>
<summary><b>Everything answers "no relevant information"</b></summary>

1. Check *Knowledge docs* — status should be **ready** with chunk count > 0
2. Lower the **similarity threshold** in *RAG settings* (default 0.5)
3. Use **🔍 Preview chunking** to confirm the document split sensibly
4. To answer questions outside your docs, enable the answer-policy switch

If the knowledge base genuinely lacks the answer, this response is **correct
behaviour**.
</details>

<details>
<summary><b>Ingestion is slow or keeps failing</b></summary>

Usually the embedding vendor's quota (Volcengine ARK's is accumulated over a
long window, in my testing).

Ingestion is **resumable** — progress is kept, so clicking "Ingest" again
continues where it stopped without re-spending quota. For large corpora run it
in the background:

```bash
cd backend
python scripts/ingest_retry.py --cooldown 60
# or detached
nohup python scripts/ingest_retry.py > ingest.log 2>&1 &
```

For much faster ingestion, switch to an embedding service with a looser quota.
Note that changing the model changes the dimension, so the index must be
**rebuilt and every document re-ingested** — on the Model config page click
**🔌 Test & detect dimension** and confirm the proposed one-click rebuild; no
manual folder deletion required.
</details>

<details>
<summary><b>Ingestion fails with <code>Collection expecting embedding with dimension of 2048, got 1024</code></b></summary>

You changed the embedding model but the existing collection is still locked to
the old model's dimension. Deleting documents or restarting the server doesn't
clear the lock (even an empty collection keeps it) — the collection itself
must be recreated.

Open the Model config page: the banner spells out the locked dimension and the
current model's dimension. Click **♻️ Rebuild vector index now** and confirm;
the backend drops and recreates the collection, then re-embeds every document
in the background. See
[Changed the embedding model? Rebuild the index in one click](#changed-the-embedding-model-rebuild-the-index-in-one-click).
</details>

<details>
<summary><b>Retrieval broke after changing vector store / embedding model</b></summary>

The vector dimension must match the model's output, and **changing the model
requires rebuilding the index and re-ingesting everything** — even with
matching dimensions, two different models live in incompatible vector spaces,
so retrieval would silently turn into noise.

Use **🔌 Test & detect dimension** — the UI reads the real dimension, syncs it
to the vector store config, and (after a confirmation) kicks off a one-click
rebuild. Watch for the amber banner at the top of the Model config page if the
dialog didn't appear.
</details>

<details>
<summary><b>Volcengine ARK returns 401</b></summary>

Check you picked the right product (subscription vs pay-as-you-go) — their base
URLs differ:

- Subscription: `/api/plan/v3` (OpenAI) or `/api/plan/v1` (Anthropic)
- Pay-as-you-go: `/api/v3`

For the subscription the model name must be `ark-code-latest`.
</details>

<details>
<summary><b>429 during chat</b></summary>

Automatic retry is built in (45s budget, exponential backoff), so you rarely
see it. If it persists, your account quota is exhausted — wait, or move to a
model with more headroom.
</details>

<details>
<summary><b>SSE stops streaming behind Nginx</b></summary>

Turn buffering off:

```nginx
location /api/chat/stream {
    proxy_pass http://127.0.0.1:8000;
    proxy_buffering off;
    proxy_cache off;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    chunked_transfer_encoding off;
}
```

The backend already sends `X-Accel-Buffering: no`.
</details>

<details>
<summary><b>Frontend changes don't show up</b></summary>

The backend sends `Cache-Control: no-store` for `/admin`, `/widget` and
`/embed`, so a normal refresh is enough. If a CDN sits in front, purge it or
bump `?v=` on the embed URL.
</details>

<details>
<summary><b>Some packages won't install on Python 3.14</b></summary>

Text splitting is a pure built-in implementation (`utils/text_splitter.py`)
with no third-party splitter dependency. Everything else is verified on
Python 3.14.5.
</details>

---

## Development

```bash
# Backend: 208 tests (73 auth, 20 missing-driver/Xinchuang-URL detection, 10 rebuild state machine)
cd backend
python -m pytest src/tests/ -q

# Widget self-test: 32 assertions — full SSE flow in a real DOM, plus region detection
cd frontend/widget
npm install       # installs jsdom
npm test

# Admin console: 58 assertions — language switching + the top-right account menu + the rebuild decision function (borrows the jsdom the widget installed)
cd frontend/admin
node selftest.mjs

# Sign-in page: 37 assertions — login success/failure, open-redirect guard, ?api= retargeting
node login-selftest.mjs

# Catalog coverage: every Chinese string in the UI must have all three translations
node i18n-check.mjs

# Environment self-check
cd backend && python scripts/check_env.py
```

> 207 of the 208 backend tests pass out of the box; the remaining failure
> predates the login work and comes from the optional `chromadb` dependency not
> being installed.

> The widget self-test earns its place: `node -c` only checks syntax and can't
> catch things like a template literal terminated early by an inner backtick,
> which breaks the whole widget at runtime. `npm test` executes the real
> rendering path.
>
> Same logic for the admin console. `i18n-check.mjs` pulls msgids out of the
> HTML with regexes, which only proves *the catalogs are complete* — it can't
> prove the key the browser computes from `innerHTML` actually matches (one
> stray space or HTML entity and the lookup misses), and it can't prove that
> switching language twice still switches back. `selftest.mjs` loads the real
> `index.html` in jsdom and runs the real `apply()`; both classes of bug were
> caught by it, not by the regex check.
>
> `login-selftest.mjs` had one obstacle worth recording: jsdom's
> `location.replace` is a read-only own property, and a successful login is
> exactly what calls it. The fix is to stop jsdom from running the page's
> scripts, pull the inline script out, and eval it wrapped in a function whose
> parameter is named `location` — the parameter shadows the global, so the
> redirect target becomes an assertable value. That is how the open-redirect
> guard (`?next=//evil.com` must land on `/admin/`) is tested.

---

## Deployment

Full guide: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** (Nginx, HTTPS,
firewall, backups, upgrades, troubleshooting). The essentials:

### Three things to know first

1. **Only Nginx should face the internet.** The app binds `127.0.0.1:8000`. The
   console does have a login, but it ships with a **default password** and a
   default `APP_SECRET_KEY` — change both before anyone can reach the box, or
   you are handing out your API key configuration page.
2. **SSE needs buffering off in Nginx** — otherwise answers appear all at once
   instead of streaming (the most common deployment mistake).
3. **API keys live in `backend/data/app.db`** (base64-obfuscated, effectively
   plaintext). It's gitignored — don't commit it.

### Docker Compose

```bash
curl -fsSL https://get.docker.com | sh
cd /opt && git clone https://github.com/vfaner/intelligent-customer-service.git intelligent-customer-service
# Or, from mainland China, use the Gitee mirror:
# git clone https://gitee.com/super_rgh/intelligent-customer-service.git intelligent-customer-service
cd intelligent-customer-service
# Create backend/Dockerfile and docker-compose.prod.yml
# (full contents in docs/DEPLOYMENT.md)
cp backend/.env.example backend/.env && nano backend/.env
docker compose -f docker-compose.prod.yml up -d --build
curl -fsS http://127.0.0.1:8000/health
```

### systemd + Nginx

```bash
sudo apt install -y python3.12 python3.12-venv nginx git
sudo useradd -r -s /bin/false -d /opt/ics csapp
sudo mkdir -p /opt/ics && cd /opt/ics && sudo git clone https://github.com/vfaner/intelligent-customer-service.git .
# Gitee mirror: sudo git clone https://gitee.com/super_rgh/intelligent-customer-service.git .
sudo chown -R csapp:csapp /opt/ics
cd backend
sudo -u csapp python3 -m venv .venv
sudo -u csapp .venv/bin/pip install -r requirements.txt
sudo -u csapp cp .env.example .env && sudo nano .env
# Write /etc/systemd/system/ics.service (template in docs/DEPLOYMENT.md)
sudo systemctl enable --now ics
```

### Nginx (required either way)

```nginx
server {
    listen 443 ssl http2;
    server_name your-domain;
    client_max_body_size 25M;

    # ⚠ SSE: buffering must be off
    location /api/chat/stream {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
        proxy_read_timeout 300s;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Protect the admin console

It has a **built-in login** (see [Login & account](#login--account)), so the
first two jobs after deployment are: **change the default password** and **set
`APP_SECRET_KEY`**.

For anything sensitive, add a second layer *on top of* the login. Most secure
first:

| Approach | How |
|---|---|
| **SSH tunnel** (recommended) | Don't expose it; `ssh -L 8000:127.0.0.1:8000 user@server`, open `localhost:8000/admin/` |
| **IP allowlist** | `allow your.ip; deny all;` on `/admin/` in Nginx |
| **Basic auth** | `htpasswd` + `auth_basic`, and cover `/api/config` `/api/admin` too |

### Security checklist

- [ ] **Default password `123456` changed** on the *Account* page
- [ ] `APP_SECRET_KEY` replaced with a random value (it signs the login tokens)
- [ ] `AUTH_ENABLED` left at `true`
- [ ] `APP_ENV=production` and `APP_DEBUG=false` (debug leaks stack traces)
- [ ] `CORS_ORIGINS` restricted to real domains, not `*`
- [ ] Admin console access-controlled
- [ ] App bound to `127.0.0.1`; DB / vector store not published
- [ ] HTTPS enabled
- [ ] Not running as root
- [ ] Nginx rate-limit on `/api/chat*` (protects your LLM bill)
- [ ] `git check-ignore -v backend/data/app.db` confirms the DB is ignored
- [ ] Backups of `backend/data/` scheduled **and restore-tested**
- [ ] Aware that document content reaches the prompt (injection risk)

---

## License

MIT
