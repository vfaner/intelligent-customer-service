# API Reference

Base URL: `http://localhost:8000`

Interactive Swagger UI: `/docs` · ReDoc: `/redoc`

---

## Meta

### `GET /health` — open
```json
{ "status": "ok", "name": "IntelligentCustomerService", "env": "development" }
```

### Static pages

These are served by the backend itself — no separate web server needed.

| Path | What |
|---|---|
| `/` | Landing page with links to everything |
| `/admin/` | **Admin console** — configure everything here. Redirects to the login page when not signed in. |
| `/admin/login.html` | Admin login. Always reachable — gating it would be a redirect loop. |
| `/admin/embed.html` | Standalone chat page. Always reachable: this is what `/embed` iframes serve to third-party sites. |
| `/embed` | Redirect to `/admin/embed.html`, preserving the query string. Use this in `<iframe src>`. |
| `/widget/customer-service.js` | The embeddable widget (19 KB, zero deps) |
| `/widget/demo/` | Embedding demo page |
| `/docs` · `/redoc` | Swagger / ReDoc |

`/embed` accepts `?api=` `&title=` `&accent=` `&position=` `&autoOpen=` `&upload=`
so the host page can theme the widget. `upload` is opt-in (`upload=1`): the
document endpoints require an admin token, so a visitor's upload would 401.

---

## Auth

Everything the admin console writes to is behind a login. The visitor-facing
slice — chat, session **creation** and read-back of a held session id, and the
public slice of the config — is not, because the embedded widget runs on
someone else's page with no credentials to offer. Enumerating or closing
other people's sessions is admin-only.

Each section below is marked **🔒 login required** or **open**.

### How the token travels

`POST /api/auth/login` returns a signed token. Send it back as:

```
Authorization: Bearer <access_token>
```

The same token is also set as an HttpOnly cookie (`cs_admin_token`), but *only*
so the server can gate the `/admin/index.html` page load before any JavaScript
runs — a browser navigation cannot attach a header. API clients should use the
header: the console can be pointed at another origin via `?api=`, and with
`CORS_ORIGINS=*` a cookie is not sent cross-origin at all.

Tokens are signed with `APP_SECRET_KEY` and carry no server-side session. They
are valid for `AUTH_TOKEN_TTL_HOURS` (default 168 = 7 days), and they embed a
fingerprint of the password hash — so changing the password invalidates every
token issued before it, on every device, with nothing to expire server-side.

Missing, malformed, expired or superseded token → `401` with the standard error
envelope.

### `GET /api/auth/state` — open

Whether login is required at all, and whether the default password is still in
use. The login page reads this to decide whether to show the default-credentials
hint. Safe to call anonymously — it discloses no account details.

```json
{ "auth_required": true, "default_password": true }
```

### `POST /api/auth/login` — open

```json
{ "username": "admin", "password": "123456" }
```
```json
{
  "access_token": "eyJ...",
  "token_type": "bearer",
  "expires_in": 604800,
  "user": { "id": 1, "username": "admin", "display_name": "管理员", "email": "",
            "is_active": true, "last_login_at": "2026-01-01T00:00:00", "created_at": "..." }
}
```

A wrong username and a wrong password return the identical message
(`用户名或密码错误`) and take the same time, so the endpoint cannot be used to
discover which accounts exist.

Brake on brute force: 5 failures within 15 minutes (counted per IP and per
IP+username pair) lock the key out for 15 minutes, doubling per further
violation up to 24 h. A locked attempt returns **429** `rate_limited` before
any password hashing runs; a successful login wipes the counter.

### `POST /api/auth/logout` — open

Clears the cookie. There is nothing server-side to revoke, so a client holding
the Bearer token must also drop it — which is what the console does.

### `GET /api/auth/me` — 🔒 login required

The signed-in account. Never includes the password hash.

### `PUT /api/auth/me` — 🔒 login required

```json
{ "username": "boss", "display_name": "老板", "email": "me@example.com" }
```

Username: 3–32 characters, no spaces, must be unique. Returns a **fresh token**
alongside the updated user, because the old one still carries the old username
in its payload. A rename does *not* sign other devices out — tokens resolve the
account by id, not by name, so they keep working under the new name.

### `PUT /api/auth/me/password` — 🔒 login required

```json
{ "old_password": "123456", "new_password": "s3cret-enough" }
```

New password: 6–128 characters, and must differ from the current one. A wrong
`old_password`, a too-short new one, or reusing the current password all come
back as `422` `validation_error`.

Also returns a **fresh token**. This is the one operation that *does* sign other
devices out: the token carries a fingerprint of the password hash, so every
token minted before the change stops resolving — including the one the caller
sent, which is why a replacement comes back in the same response.

### `GET /api/auth/ping` — 🔒 login required

A cheap "is my token still good?" check. Returns `{ "ok": true }`.

---

## Admin（安装向导）— 🔒 login required

这两个端点驱动管理后台顶部的「首次运行向导」横幅。

`GET /api/admin/status` 的 `details` 里多了一个 `default_password_in_use`，
后台据此显示「请尽快修改默认密码」横幅。`POST /api/admin/init` 除了建表和写入
默认配置，还会创建初始管理员账号。

### `GET /api/admin/status`

检查系统是否可用，返回缺什么。

```json
{
  "ready": false,
  "issues": ["客服配置未初始化"],
  "fixes": ["seed_config"],
  "details": {
    "db_ready": true,
    "has_config": false,
    "has_documents": false,
    "doc_count": 0,
    "upload_dir": "uploads",
    "upload_dir_exists": true,
    "chroma_dir": "data/chroma_db",
    "chroma_dir_exists": true,
    "vector_db_provider": "chroma",
    "llm_provider": "deepseek",
    "llm_api_key_set": true,
    "embedding_api_key_set": true
  }
}
```

`llm_api_key_set` / `embedding_api_key_set` 会同时检查 `.env` **和**数据库里的配置。

### `POST /api/admin/init`

一键初始化：建表 + 建运行目录 + 写入默认客服配置。**幂等**，可重复调用。

```bash
curl -X POST http://localhost:8000/api/admin/init
```

```json
{
  "ok": true,
  "message": "初始化完成",
  "actions": [
    "创建上传目录: uploads",
    "创建 Chroma 目录: data/chroma_db",
    "创建数据库表 (cs_config / cs_session / cs_message / cs_document)",
    "写入默认客服配置（provider: deepseek, model: deepseek-chat）"
  ],
  "details": { "tables_created": true, "seeded_config": true }
}
```

---

## Config

### `GET /api/config` — 🔒 login required
Returns the current customer-service config. All secrets (`model_api_key`,
`embedding.api_key`, `vector_db.milvus_token`) come back **masked** as
`****1234`. When no DB row exists yet, the `.env` values are surfaced instead
so the admin form isn't blank.

### `PUT /api/config` — 🔒 login required
Update config. Send the full object (do a `GET` first, spread, then modify).

**Secret handling**: leaving a secret blank *or* echoing back the `****1234`
mask keeps the stored value. Only a genuinely new string replaces it — so the
admin UI can save other fields without ever seeing the real key.

```json
{
  "name": "智能客服小助手",
  "avatar": "",
  "contact_phone": "",
  "contact_email": "",
  "welcome_message": "您好，请问有什么可以帮您？",

  "model_provider": "deepseek",
  "model_name": "deepseek-chat",
  "model_api_key": "sk-xxx",
  "model_base_url": "https://api.deepseek.com/v1",
  "protocol": "openai",
  "temperature": 0.3,
  "max_tokens": 2048,

  "embedding": {
    "provider": "openai_compatible",
    "api_key": "sk-xxx",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "text-embedding-v3",
    "dim": 1024
  },

  "vector_db": {
    "provider": "chroma",
    "host": "localhost",
    "port": 6333,
    "collection": "knowledge_base",
    "embedding_dimension": 1024,
    "persist_directory": "./data/chroma_db",
    "milvus_uri": "./data/milvus.db",
    "milvus_token": "",
    "milvus_db_name": ""
  },

  "rag": {
    "top_k": 5,
    "similarity_threshold": 0.5,
    "chunk_size": 500,
    "chunk_overlap": 100,
    "chunk_strategy": "structure",
    "chunk_prefix_heading": true
  },

  "active_provider_code": "deepseek"
}
```

Changes take effect on the next request — no restart needed. (Switching
`vector_db.provider` does require a restart plus re-ingesting documents.)

### `GET /api/config/public` — open

The visitor-safe slice of 客服信息, and the only config endpoint the embedded
widget touches:

```json
{
  "name": "智能客服小助手",
  "avatar": "",
  "welcome_message": "您好，请问有什么可以帮您？",
  "contact_phone": "",
  "contact_email": ""
}
```

Five fields, nothing else. `GET /api/config` also carries API base URLs, model
names, RAG parameters and vector-database coordinates — which is exactly why it
requires a login and this one exists.

### `GET /api/config/providers` — 🔒 login required

Returns the vendor registry (15 entries). Each entry:

```json
{
  "code": "volcengine_plan",
  "display_name": "火山方舟 · 包月套餐（Coding Plan）",
  "protocols": ["openai", "anthropic"],
  "endpoints": [
    { "protocol": "openai",    "base_url": "https://ark.cn-beijing.volces.com/api/plan/v3", "default_model": "ark-code-latest" },
    { "protocol": "anthropic", "base_url": "https://ark.cn-beijing.volces.com/api/plan/v1", "default_model": "ark-code-latest" }
  ],
  "default_embedding_model": "",
  "recommended_models": ["ark-code-latest"],
  "supports_model_listing": false,
  "notes": "包月套餐专用地址…",
  "is_custom": false,
  "verified": true
}
```

| Field | Meaning |
|---|---|
| `endpoints` | One entry per protocol the vendor speaks. Switching protocol in the UI swaps `base_url` + `default_model` from here. |
| `recommended_models` | Curated fallback list, used when `/api/models/available` can't reach the vendor. |
| `supports_model_listing` | `false` → don't even try `GET {base}/models`. |
| `is_custom` | `true` → the UI must ask for `base_url` and `model` (no presets). |
| `verified` | `false` → preset values are **unconfirmed**; the UI shows an orange warning. |

Legacy codes (`qwen`, `bailian`, `doubao`, `volcengine`, `volcengine_ark`,
`volcengine_anthropic`) are transparently mapped onto their current entries, so
configs saved by older versions keep working.

---

## Documents — 🔒 login required

All of them. Ingesting into the knowledge base is an operator task, so the
widget ships with `enableUpload: false`: a visitor pressing an upload button
would only ever see a `401`.

### `POST /api/documents/upload`
Multipart upload. Accepts `.txt`, `.docx`, `.xlsx`, `.pdf` (max 20 MB).

```bash
curl -F "file=@samples/产品手册.txt" http://localhost:8000/api/documents/upload
```

Response:
```json
{
  "id": "a1b2c3...",
  "filename": "产品手册.txt",
  "file_size": 12345,
  "file_md5": "...",
  "mime_type": "text/plain",
  "parser_code": "txt",
  "status": "uploaded",
  "chunk_count": 0
}
```

### `POST /api/documents/process`
Queues chunking + embedding + indexing as a **background job** and returns
`202 Accepted` immediately (a large document takes minutes; the old
synchronous behaviour died on proxy/browser timeouts). The response body is
the current document row — normally `"status": "processing"`. Poll
`GET /api/documents/list` for progress: `chunks_done` / `chunks_total` update
while the job runs, and `status` ends as `ready` or `failed` (with
`error_message`).

Submitting the same document while its job is still running returns `400`
(`document_error`). If the server restarts mid-ingest, the row is marked
`failed` after 15 stale minutes and a re-submit **resumes** from the last
indexed chunk instead of re-embedding (and re-paying for) it.

```bash
curl -X POST http://localhost:8000/api/documents/process \
  -H "Content-Type: application/json" \
  -d '{"document_id": "a1b2c3...", "chunk_size": 500, "chunk_overlap": 100}'
```

### `GET /api/documents/list`
Returns all uploaded documents with status.

### `DELETE /api/documents/{id}`
Removes the document and its vectors.

### `GET /api/documents/parsers`
Returns supported file formats.

### `POST /api/documents/split-preview`

Chunk text **without** embedding or indexing it — used by the admin UI to let
you tune chunk size / strategy and see the result immediately.

Pass either `text` (pasted) or `document_id` (already uploaded):

```bash
curl -X POST http://localhost:8000/api/documents/split-preview \
  -H "Content-Type: application/json" \
  -d '{"document_id": "abc123", "strategy": "structure"}'
```

| Field | Default | Meaning |
|---|---|---|
| `text` | — | Raw text to split |
| `document_id` | — | Split an uploaded document instead |
| `chunk_size` / `chunk_overlap` | saved config | Override for this preview only |
| `strategy` | saved config | `structure` \| `window` |
| `prefix_heading` | saved config | Prepend the heading path to each chunk |

Response:

```json
{
  "source": "sample_knowledge.txt",
  "strategy": "structure",
  "chunk_size": 500,
  "chunk_overlap": 100,
  "prefix_heading": true,
  "total_chars": 595,
  "chunk_count": 6,
  "avg_chars": 97,
  "min_chars": 9,
  "max_chars": 260,
  "chunks": [
    { "index": 2, "heading": "保修条款", "chars": 105, "text": "【保修条款】\n1. 整机保修期为 12 个月…" }
  ],
  "truncated": false
}
```

At most 60 chunks are returned; `truncated` says whether more exist.

### `POST /api/documents/reindex`

Rebuild the whole vector index with the **current embedding config**. Use this
after changing the embedding model: the collection is bound to the model that
created it (dimension lock + embedding space), so old vectors must be dropped
and every document re-embedded.

The job runs in the background: probe the embedding endpoint → drop & recreate
the collection at the probed dimension → reset all document rows → re-ingest
each document with rate-limit backoff and resumable checkpoints. Returns
`409 {"detail": "向量库重建任务已在运行中"}` while a job is already running.

```bash
curl -X POST http://localhost:8000/api/documents/reindex \
  -H "Authorization: Bearer <token>"
```

### `GET /api/documents/reindex/status`

Job state plus the model-signature mismatch that drives the console banner:

```json
{
  "running": false,
  "phase": "idle",
  "repair_mode": false,
  "docs_total": 40,
  "docs_done": 12,
  "current_file": "faq.md",
  "failed": [],
  "error": "",
  "started_at": "",
  "finished_at": "",
  "docs_ready": 0,
  "vector_count": 0,
  "store_dimension": 2048,
  "current_signature": {"provider": "openai_compatible", "model": "text-embedding-v3",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "dim": 1024},
  "index_signature": null,
  "needs_rebuild": true
}
```

`phase` is one of `idle` / `preflight` / `reset` / `indexing` / `done` /
`failed`. `needs_rebuild` is true when the live collection is locked to another
dimension (even with zero documents) or documents carry a different model
signature; it is forced false while a job is running.

---

## Chat — open

No login: this is what the embedded widget calls from a stranger's browser.

### `POST /api/chat`
Non-streaming RAG chat.

```json
{
  "session_id": "optional-id",
  "message": "产品的保修期是多久？",
  "agent_name": "智能客服小助手",
  "lang": "ja"
}
```

`lang` (`zh-CN` / `zh-TW` / `ja` / `en`, default `zh-CN`) pins the **answer's**
language. The knowledge base stays Chinese and retrieval is unchanged — the
Chinese system prompt is reused verbatim and an output-language directive,
written in the target language, is appended to it. Unknown or absent values fall
back to `zh-CN`, whose prompt is byte-identical to the pre-i18n one.

Localised alongside the answer: the canned "not in the knowledge base" refusal,
the default agent name, the auto-generated session title, and error prefixes.

Response:
```json
{
  "session_id": "...",
  "answer": "根据资料 [1]，保修期为 12 个月。",
  "sources": [
    {
      "index": 1,
      "chunk_id": "abc123:2",
      "text": "【保修条款】\n1. 整机保修期为 12 个月…",
      "score": 0.7907,
      "document_id": "abc123",
      "filename": "sample_knowledge.txt",
      "heading": "保修条款",
      "chunk_index": 2
    }
  ]
}
```

`score` is normalised to `0..1` across all three vector stores, so the
similarity threshold means the same thing regardless of backend.
`heading` is the section the chunk came from (empty when the document has no
recognisable structure).

`session_id` is validated: an unknown id returns **404** `not_found` (it used
to be accepted blindly, orphaning every message), and a session an admin has
closed returns **422**. Omit it to start a new session — the response carries
the generated id. A client-supplied `history` (API consumers only; the widget
relies on the server-side session) accepts `user`/`assistant` roles only and
is capped at 20 messages / 6000 characters, mirroring the server-side trim.

### `POST /api/chat/stream`
Server-Sent Events stream of the same call.

SSE events:
- `event: meta` — first, carries `{session_id, lang}`
- `event: token` — incremental text `{text}`
- `event: sources` — final, the matched chunks array
- `event: done` — stream end `{ok: true|false}`
- `event: error` — on failure `{message}`

```bash
curl -N -X POST http://localhost:8000/api/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message":"产品的保修期是多久？","lang":"en"}'
```

---

## Sessions — mixed

Creating a session and reading back the messages of a session id you already
hold are open — that is what lets the widget resume its own conversation
without a credential. **Listing, inspecting and closing** sessions is 🔒 login
required: these used to be open, which let any anonymous visitor dump every
customer conversation.

### `POST /api/sessions` — open
Create a session. Body: `{user_id?, title?, meta?}`.

### `GET /api/sessions/{id}/messages?limit=200` — open
The most recent `limit` messages of one session, oldest first. A capability
URL: whoever holds the id can read that thread back.

### `GET /api/sessions?user_id=&limit=` — 🔒 login required
List sessions, most recently updated first.

### `GET /api/sessions/{id}` — 🔒 login required
Session metadata (the title is the first question's first 30 chars).

### `POST /api/sessions/{id}/close` — 🔒 login required
Mark a session as closed. A closed session rejects new chat turns with 422.

---

## Models — 🔒 login required

All of them. These read and probe your API keys and vector-database endpoints.

### `GET /api/models`
Returns both LLM providers and vector DB providers.

### `GET /api/models/vector-db`
Returns vector DB providers only.

### `POST /api/models/test`
Smoke-test the LLM connection. Accepts any of:
`{provider?, model?, api_key?, base_url?, protocol?}`. Missing fields fall back to the active `CSConfig`.

```bash
curl -X POST http://localhost:8000/api/models/test \
  -H "Content-Type: application/json" \
  -d '{"provider": "deepseek", "api_key": "sk-xxx", "model": "deepseek-chat"}'
```

Response:
```json
{ "ok": true, "message": "测试成功 - 模型回复: OK" }
```

### `POST /api/models/test-embedding`

Embed a probe string and report the **actual vector dimension** — use this to
get the number that `VECTOR_DB_EMBEDDING_DIM` must match.

```bash
curl -X POST http://localhost:8000/api/models/test-embedding \
  -H "Content-Type: application/json" -d '{}'
```

```json
{
  "ok": true,
  "message": "测试成功 - 模型 text-embedding-v3 返回 1024 维向量",
  "dim": 1024,
  "configured_dim": 1024
}
```

If `dim != configured_dim` the message warns you, and the admin UI
auto-corrects both the embedding and vector-DB dimension settings.

Omitted fields (`api_key` / `base_url` / `model`) fall back to the saved config.

### `POST /api/models/available`

List the models a vendor actually offers, by calling its `GET {base}/models`.

```bash
curl -X POST http://localhost:8000/api/models/available \
  -H "Content-Type: application/json" \
  -d '{"provider": "dashscope", "protocol": "openai"}'
```

```json
{
  "ok": true,
  "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
  "source": "api",
  "message": "已从厂商接口获取 42 个模型"
}
```

`source` tells you where the list came from:

| `source` | When |
|---|---|
| `api` | Fetched live from the vendor |
| `recommended` | Fell back to the built-in curated list — missing key, vendor has no listing endpoint, or the call failed. `message` explains which. |

Chat models are sorted first; embedding / tts / whisper / image entries are
pushed to the end. This endpoint **never fails hard** — it always returns a
usable list so the UI dropdown is never empty.

---

## Error envelope

All errors use:
```json
{
  "error": {
    "code": "document_error",
    "message": "Unsupported file type: foo.exe",
    "details": {}
  }
}
```

| HTTP | code                | When |
|------|---------------------|------|
| 400  | `document_error`    | Bad file / unsupported type |
| 404  | `not_found`         | Missing session / document |
| 422  | `validation_error`  | Bad input shape |
| 500  | `vector_store_error` / `internal_error` | Server-side issue |
| 502  | `llm_error` / `embedding_error` | Upstream API failure |
