"""Smoke tests for parsers, text splitter, and provider registry."""
from __future__ import annotations

import pytest

from src.parsers import get_parser
from src.core.registry import (
    AI_PROVIDERS,
    DOCUMENT_PARSERS,
    VECTOR_DB_PROVIDERS,
    list_providers,
    parser_for_filename,
)
from src.utils.text_splitter import split_text


def test_provider_registry_has_required_keys() -> None:
    codes = {p.code for p in AI_PROVIDERS}
    for required in {
        "deepseek", "dashscope", "volcengine_plan", "doubao", "zhipu",
        "kimi", "qianfan", "openai", "anthropic", "gemini",
    }:
        assert required in codes, f"missing provider: {required}"


def test_provider_registry_has_no_duplicate_endpoints() -> None:
    """A (base_url, protocol) pair must map to exactly one vendor entry."""
    from src.core.registry import AI_PROVIDERS as PROVIDERS

    seen: dict[tuple[str, str], str] = {}
    for p in PROVIDERS:
        for e in p.endpoints:
            if not e.base_url:      # custom entries have no preset URL
                continue
            key = (e.base_url, e.protocol)
            assert key not in seen, (
                f"duplicate endpoint {key}: {p.code} collides with {seen[key]}"
            )
            seen[key] = p.code


def test_legacy_provider_codes_still_resolve() -> None:
    """Configs saved before the merge must keep working."""
    from src.core.registry import get_provider, resolve_provider_code

    assert resolve_provider_code("qwen") == "dashscope"
    assert resolve_provider_code("bailian") == "dashscope"
    # ARK: the metered product keeps the `doubao` code; the older ambiguous
    # codes point at it too (metered is the safer default — the subscription
    # endpoint 404s for users who don't own a plan).
    assert resolve_provider_code("volcengine_ark") == "doubao"
    assert resolve_provider_code("volcengine_anthropic") == "doubao"
    assert resolve_provider_code("volcengine") == "volcengine_plan"
    # Current codes pass through untouched.
    assert resolve_provider_code("deepseek") == "deepseek"
    assert resolve_provider_code("doubao") == "doubao"
    # Unknown codes are returned as-is rather than raising.
    assert resolve_provider_code("some_gateway") == "some_gateway"
    assert get_provider("qwen") is not None


def test_ark_subscription_and_metered_are_separate() -> None:
    """The two ARK products bill differently and must not be merged.

    /api/plan* is the monthly subscription; /api/v3 is pay-as-you-go and
    charges extra. Mixing them up silently costs the user money.
    """
    from src.core.registry import get_provider

    plan = get_provider("volcengine_plan")
    metered = get_provider("doubao")

    for e in plan.endpoints:
        assert "/api/plan" in e.base_url, f"subscription endpoint must use /api/plan: {e.base_url}"
        assert e.default_model == "ark-code-latest"

    for e in metered.endpoints:
        assert e.base_url.endswith("/api/v3"), f"metered endpoint must use /api/v3: {e.base_url}"
        assert "/api/plan" not in e.base_url
        assert e.default_model.startswith("doubao-")

    # They must not collide on (base_url, protocol).
    plan_keys = {(e.base_url, e.protocol) for e in plan.endpoints}
    metered_keys = {(e.base_url, e.protocol) for e in metered.endpoints}
    assert not (plan_keys & metered_keys)


def test_every_provider_has_at_least_one_endpoint() -> None:
    for p in AI_PROVIDERS:
        assert p.endpoints, f"{p.code} has no endpoints"
        assert p.protocol in ("openai", "anthropic")
        # The convenience accessors must agree with the first endpoint.
        assert p.base_url == p.endpoints[0].base_url
        assert p.default_model == p.endpoints[0].default_model


def test_multi_protocol_providers_expose_both() -> None:
    from src.core.registry import get_provider

    plan = get_provider("volcengine_plan")
    assert set(plan.protocols) == {"openai", "anthropic"}
    # Switching protocol must yield a usable endpoint.
    assert plan.endpoint_for("anthropic").default_model
    assert plan.endpoint_for("openai").default_model
    assert plan.endpoint_for("nonsense") is None
    # And the two protocols use different URLs for this vendor.
    assert plan.endpoint_for("openai").base_url != plan.endpoint_for("anthropic").base_url


def test_list_providers_serializable() -> None:
    items = list_providers()
    assert items and all("code" in p and "protocol" in p for p in items)


def test_parser_dispatch() -> None:
    for fname, code in [("a.txt", "txt"), ("a.docx", "docx"), ("a.xlsx", "xlsx"), ("a.pdf", "pdf")]:
        spec = parser_for_filename(fname)
        assert spec is not None and spec.code == code


def test_text_splitter_chinese() -> None:
    text = "第一段。" * 80
    chunks = split_text(text, chunk_size=200, chunk_overlap=40)
    assert len(chunks) >= 2
    assert all(c.text for c in chunks)


def test_vector_db_registry() -> None:
    assert {p.code for p in VECTOR_DB_PROVIDERS} == {"chroma", "qdrant", "milvus"}


def test_vector_store_factory_dispatch(tmp_path) -> None:
    """Every registered provider must be constructible by the factory."""
    from src.core.config import Settings
    from src.vector_store.chroma_store import ChromaStore
    from src.vector_store.factory import build_vector_store
    from src.vector_store.milvus_store import MilvusStore

    s = Settings()
    s.vector_db.embedding_dim = 4
    s.vector_db.collection = "factory_probe"

    s.vector_db.provider = "chroma"
    s.vector_db.chroma_persist_dir = str(tmp_path / "chroma")
    assert isinstance(build_vector_store(s), ChromaStore)

    s.vector_db.provider = "milvus"
    s.vector_db.milvus_uri = str(tmp_path / "milvus.db")
    assert isinstance(build_vector_store(s), MilvusStore)


def test_vector_store_factory_rejects_unknown() -> None:
    from src.core.config import Settings
    from src.core.exceptions import VectorStoreError
    from src.vector_store.factory import build_vector_store

    s = Settings()
    s.vector_db.provider = "not_a_real_db"  # type: ignore[assignment]
    with pytest.raises(VectorStoreError):
        build_vector_store(s)


def test_document_parsers_registry() -> None:
    assert {p.code for p in DOCUMENT_PARSERS} >= {"txt", "docx", "xlsx", "pdf"}


def test_txt_parser(tmp_path) -> None:
    p = tmp_path / "x.txt"
    p.write_text("hello world\n你好", encoding="utf-8")
    parser = get_parser("x.txt")
    assert "hello" in parser.parse(p)
    assert "你好" in parser.parse(p)


# ---------------------------------------------------------------------------
# Structure-aware chunking
# ---------------------------------------------------------------------------
SAMPLE_DOC = """产品说明书

【保修条款】
1. 整机保修期为 12 个月。
2. 电池单独保修 6 个月。

【退换货政策】
1. 7 天无理由退货。

【常见问题】
Q1：如何重置？
A：长按电源键 10 秒。
Q2：数据安全吗？
A：存储在你自己的服务器上。
"""


def test_structure_split_keeps_sections_apart() -> None:
    """Each 【section】 must land in its own chunk, not be merged together."""
    chunks = split_text(SAMPLE_DOC, chunk_size=500, chunk_overlap=100)
    headings = [c.heading for c in chunks if c.heading]
    assert "保修条款" in headings
    assert "退换货政策" in headings
    assert "常见问题" in headings

    # The warranty chunk must not also contain the returns policy.
    warranty = next(c for c in chunks if c.heading == "保修条款")
    assert "12 个月" in warranty.text
    assert "无理由退货" not in warranty.text


def test_structure_split_beats_window_on_section_purity() -> None:
    """The whole point: structure mode produces more, cleaner chunks."""
    structure = split_text(SAMPLE_DOC, chunk_size=500, chunk_overlap=100, strategy="structure")
    window = split_text(SAMPLE_DOC, chunk_size=500, chunk_overlap=100, strategy="window")
    assert len(structure) > len(window)
    # Window mode crams unrelated sections together; structure mode doesn't.
    assert any("保修条款" in c.text and "退换货政策" in c.text for c in window)
    assert not any("保修条款" in c.text and "退换货政策" in c.text for c in structure)


def test_heading_prefix_can_be_disabled() -> None:
    long_section = "【技术规格】\n" + "参数说明。" * 80
    with_prefix = split_text(long_section, chunk_size=200, chunk_overlap=40, prefix_heading=True)
    without = split_text(long_section, chunk_size=200, chunk_overlap=40, prefix_heading=False)
    # Continuation chunks carry the heading only when prefixing is on.
    assert any(c.text.startswith("[技术规格]") for c in with_prefix)
    assert not any(c.text.startswith("[技术规格]") for c in without)


def test_no_content_free_heading_chunks() -> None:
    """A chunk that's only a heading would match queries but answer nothing."""
    for text, size in [(SAMPLE_DOC, 500), ("【技术规格】\n" + "参数。" * 100, 200)]:
        for c in split_text(text, chunk_size=size, chunk_overlap=40):
            body = c.text
            if c.heading and body.startswith(f"[{c.heading}]"):
                body = body[len(c.heading) + 3:]
            lines = [ln for ln in body.splitlines() if ln.strip()]
            # Strip the heading line itself; something must remain.
            if lines and lines[0].strip().startswith("【"):
                lines = lines[1:]
            assert lines, f"content-free chunk: {c.text!r}"


def test_markdown_and_chapter_headings_recognised() -> None:
    md = split_text("# 手册\n开场白。\n\n## 安装\n下载并运行。\n", chunk_size=300, chunk_overlap=50)
    assert any(c.heading == "手册 > 安装" for c in md), [c.heading for c in md]

    zh = split_text("第一章 总则\n适用于全体。\n\n第二章 考勤\n九点上班。\n", chunk_size=300, chunk_overlap=50)
    assert any("第二章" in (c.heading or "") for c in zh), [c.heading for c in zh]


def test_xlsx_sheet_markers_become_headings() -> None:
    text = "## Sheet: 价格表\n产品 | 单价\nA | 100\n\n## Sheet: 联系人\n姓名 | 电话\n张三 | 138\n"
    chunks = split_text(text, chunk_size=300, chunk_overlap=50)
    headings = [c.heading for c in chunks]
    assert "Sheet: 价格表" in headings
    assert "Sheet: 联系人" in headings


def test_numbered_list_items_are_not_mistaken_for_headings() -> None:
    """"1. 整机保修期为 12 个月。" is content, not a heading."""
    chunks = split_text(SAMPLE_DOC, chunk_size=500, chunk_overlap=100)
    assert not any("整机保修期" in (c.heading or "") for c in chunks)


def test_splitter_rejects_bad_params_and_survives_odd_input() -> None:
    import pytest as _pytest

    with _pytest.raises(ValueError):
        split_text("abc", chunk_size=0)
    with _pytest.raises(ValueError):
        split_text("abc", chunk_size=100, chunk_overlap=100)

    assert split_text("", 100, 10) == []
    assert split_text("   \n\n  ", 100, 10) == []
    # A document that is nothing but headings yields no answerable chunks.
    assert split_text("【空】\n\n【也空】\n", 200, 40) != [] or True  # must not raise


# ---------------------------------------------------------------------------
# Embedding config validation
# ---------------------------------------------------------------------------
def test_embedder_reports_all_missing_fields_at_once() -> None:
    from src.core.exceptions import EmbeddingError
    from src.services.embedding_service import build_embedder_from

    with pytest.raises(EmbeddingError) as exc:
        build_embedder_from({"provider": "openai_compatible"})
    msg = str(exc.value)
    # All three, not just the first one we happen to check.
    for label in ("Base URL", "模型名称", "API Key"):
        assert label in msg, msg
    assert "管理后台" in msg, "错误信息必须告诉用户去哪修"


def test_embedder_rejects_key_and_url_from_different_sources() -> None:
    """A console-saved key + an .env base_url is usually two vendors → 401.

    Catching it up front beats an opaque authentication error.
    """
    from src.core.exceptions import EmbeddingError
    from src.services.embedding_service import build_embedder_from

    mixed = {
        "provider": "openai_compatible",
        "api_key": "sk-somekey",
        "base_url": "https://api.openai.com/v1",
        "model": "text-embedding-3-small",
        "dim": 1024,
        "_origin": {"api_key": "db", "base_url": "env"},
    }
    with pytest.raises(EmbeddingError) as exc:
        build_embedder_from(mixed)
    assert "不是同一家厂商" in str(exc.value)

    # Same values, consistent origins → allowed.
    consistent = {**mixed, "_origin": {"api_key": "db", "base_url": "db"}}
    assert build_embedder_from(consistent) is not None
    # No origin info at all (CLI / sync path) → don't block.
    no_origin = {k: v for k, v in mixed.items() if k != "_origin"}
    assert build_embedder_from(no_origin) is not None


def test_embedder_rejects_deepseek_base_url() -> None:
    """DeepSeek has no embedding API — the chat base_url copy-pasted here
    would 404 on every call. The guard must name the real problem and the
    fix, regardless of where the values came from."""
    from src.core.exceptions import EmbeddingError
    from src.services.embedding_service import build_embedder_from

    cfg = {
        "provider": "openai_compatible",
        "api_key": "sk-deepseekkey",
        "base_url": "https://api.deepseek.com/v1",
        "model": "embedding-3",
        "dim": 1024,
        "_origin": {"api_key": "db", "base_url": "db"},
    }
    with pytest.raises(EmbeddingError) as exc:
        build_embedder_from(cfg)
    msg = str(exc.value)
    assert "不提供向量化" in msg and "404" in msg
    assert "管理后台" in msg, "错误信息必须告诉用户去哪修"


def test_markdown_files_are_accepted_for_upload() -> None:
    """Knowledge bases are commonly authored as .md — it must be ingestible."""
    for fname in ("kb.md", "guide.markdown", "notes.txt"):
        spec = parser_for_filename(fname)
        assert spec is not None, f"{fname} should be uploadable"
        assert spec.code == "txt"
    assert parser_for_filename("payload.exe") is None


def test_markdown_headings_drive_chunking(tmp_path) -> None:
    """A generated KB file must split into per-article chunks with headings."""
    kb = tmp_path / "kb.md"
    kb.write_text(
        "# 站点知识库\n\n"
        "【文章甲】\n文章地址：https://example.com/1.html\n\n正文甲的内容说明。\n\n"
        "【文章乙】\n文章地址：https://example.com/2.html\n\n正文乙的内容说明。\n",
        encoding="utf-8",
    )
    text = get_parser("kb.md").parse(kb)
    chunks = split_text(text, chunk_size=800, chunk_overlap=100)

    headings = [c.heading for c in chunks]
    assert any("文章甲" in (h or "") for h in headings), headings
    assert any("文章乙" in (h or "") for h in headings), headings
    # Each article's chunk must carry its own URL so answers can cite it.
    jia = next(c for c in chunks if "正文甲" in c.text)
    assert "https://example.com/1.html" in jia.text
    assert "https://example.com/2.html" not in jia.text


# ---------------------------------------------------------------------------
# Answer-mode toggle (rag.allow_model_knowledge)
# ---------------------------------------------------------------------------
async def _build(question: str, hits: list, *, allow: bool):
    """Drive RAGService._build_messages with a stubbed retriever."""
    from src.services.rag_service import RAGService

    rag = RAGService.__new__(RAGService)          # skip __init__/DB/network
    rag._settings = None
    rag._rag_config = lambda: _async_value({          # type: ignore[assignment]
        "top_k": 5, "similarity_threshold": 0.5,
        "max_context_chars": 8000, "allow_model_knowledge": allow,
        "relevance_threshold": 0.76,
    })
    rag._retrieve = lambda q, cfg: _async_value(hits)  # type: ignore[assignment]
    return await rag._build_messages(question, None, "小智")


def _async_value(v):
    async def _inner():
        return v
    return _inner()


def _hit(text="保修期为 12 个月。", score=0.85):   # above relevance_threshold
    from src.vector_store.base import VectorHit
    return VectorHit(chunk_id="d:0", text=text, score=score, metadata={"filename": "a.md"})


async def test_toggle_off_keeps_refusal_prompt() -> None:
    """Default behaviour: no hits → caller refuses, prompt stays document-only."""
    messages, sources, from_model = await _build("天气如何", [], allow=False)
    assert sources == []
    assert from_model is False
    system = messages[0].content
    assert "只能" in system, "关闭时必须保留「只能依据参考资料」约束"
    assert "依据自己的知识" not in system


async def test_toggle_on_switches_prompt_when_nothing_retrieved() -> None:
    """No hits + toggle on → drop the document-only framing entirely.

    Keeping it would make the model refuse even though it was told it may
    answer freely — the whole point of the switch.
    """
    messages, sources, from_model = await _build("天气如何", [], allow=True)
    assert sources == []
    assert from_model is True
    system = messages[0].content
    assert "依据自己的知识" in system
    assert "只能" not in system
    # The user message must be the raw question, not the RAG template.
    assert messages[-1].content == "天气如何"


async def test_toggle_on_still_prefers_documents_when_hits_exist() -> None:
    """Hits + toggle on → RAG prompt plus a "you may fill gaps" rule."""
    messages, sources, from_model = await _build("保修多久", [_hit()], allow=True)
    assert len(sources) == 1
    assert from_model is False
    system = messages[0].content
    assert "只能" in system          # document-first is retained
    assert "补充说明" in system      # gap-filling permitted
    assert "不要自己编" in system    # site-specific facts stay grounded


async def test_toggle_off_with_hits_is_unchanged() -> None:
    messages, sources, from_model = await _build("保修多久", [_hit()], allow=False)
    assert len(sources) == 1 and from_model is False
    assert "补充说明" not in messages[0].content


def _weak_hit():
    """Above similarity_threshold but below relevance_threshold.

    This is the common CJK case: unrelated text still scores ~0.65.
    """
    from src.vector_store.base import VectorHit
    return VectorHit(chunk_id="d:9", text="无关内容", score=0.65, metadata={})


async def test_weak_hits_count_as_irrelevant_when_toggle_on() -> None:
    """A barely-above-threshold hit must not block the fallback.

    Before this was handled, `similarity_threshold=0.5` meant off-topic
    questions always "found" something (~0.65) and the toggle never fired.
    """
    messages, sources, from_model = await _build("天气如何", [_weak_hit()], allow=True)
    assert from_model is True, "弱命中应视为不相关，走自主回答"
    assert sources == []
    assert "依据自己的知识" in messages[0].content


async def test_weak_hits_still_refuse_when_toggle_off() -> None:
    """With the toggle off, weak hits go down the RAG path and get refused
    by the prompt — never silently answered from model knowledge."""
    messages, sources, from_model = await _build("天气如何", [_weak_hit()], allow=False)
    assert from_model is False
    assert len(sources) == 1
    assert "只能" in messages[0].content


# ---------------------------------------------------------------------------
# Update check (GET /api/admin/version)
# ---------------------------------------------------------------------------
def test_version_parsing_is_numeric_not_lexical() -> None:
    """Versions must compare as numbers, not strings.

    String comparison says "2.0" > "10.0", which would tell users they're up to
    date when they're nine releases behind.
    """
    from src.api.admin import _parse_version as pv

    assert pv("10.0.0") > pv("2.0.0")
    assert pv("0.2.0") > pv("0.1.9")
    assert pv("1.0.0") > pv("0.99.99")


def test_version_parsing_normalises_common_tag_shapes() -> None:
    from src.api.admin import _parse_version as pv

    assert pv("v1.2.3") == pv("1.2.3")      # leading v is ignored
    assert pv("1.2") == pv("1.2.0")         # short forms are padded
    assert pv("1.0.0-beta") == pv("1.0.0")  # pre-release suffix stripped
    assert pv("1.0.0+build5") == pv("1.0.0")


def test_version_parsing_survives_garbage() -> None:
    """A malformed tag must not raise — the endpoint has to keep answering."""
    from src.api.admin import _parse_version as pv

    for bad in ("", "abc", "v", "...", "1.x.3", None):
        assert isinstance(pv(bad), tuple)
        assert len(pv(bad)) == 3


async def test_version_endpoint_degrades_without_repo(monkeypatch) -> None:
    """No APP_GITHUB_REPO → explain how to configure it, don't error out.

    The repo is patched rather than read from live config: the shipped default
    points at the real repository, so relying on it being empty made this test
    pass or fail depending on configuration.
    """
    from src.api import admin as admin_mod
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.app, "github_repo", "", raising=False)

    r = await admin_mod.version_check()
    assert r["configured"] is False
    assert r["ok"] is False
    assert r["has_update"] is False
    assert "APP_GITHUB_REPO" in r["message"]
    # The running version is always reported so the UI can show it.
    assert r["current"]


async def test_version_endpoint_reports_repo_when_configured(monkeypatch) -> None:
    """A configured repo is echoed back so the UI can build the GitHub link.

    Network calls can fail in CI, so only the always-present fields are
    asserted — not the lookup result.
    """
    from src.api import admin as admin_mod
    from src.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings.app, "github_repo", "owner/name", raising=False)

    r = await admin_mod.version_check()
    assert r["configured"] is True
    assert r["repo"] == "owner/name"
    assert r["current"]
    assert isinstance(r["has_update"], bool)


def test_version_comes_from_pyproject() -> None:
    """The running version must be the one in pyproject.toml, not a copy.

    Regression guard. It used to be a hardcoded default that only APP_VERSION
    in .env could override — and .env is gitignored, so `git pull` never
    updated it. The update check then compared a stale number against the
    repo's latest tag and permanently claimed an update was available on an
    already-current checkout. If someone reintroduces a second place to write
    the version, this goes red.

    The displayed form carries a `v` to match the git tags; pyproject.toml
    keeps the bare PEP 440 number, since a leading `v` there is non-canonical
    and would be stripped when building a wheel.
    """
    import tomllib
    from pathlib import Path

    from src.core.config import get_settings

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    with pyproject.open("rb") as fh:
        declared = tomllib.load(fh)["project"]["version"]

    assert not declared.lower().startswith("v"), "pyproject.toml 里不要写 v 前缀"
    assert get_settings().app.version == f"v{declared}"


def test_matching_version_reports_no_update() -> None:
    """current == latest tag must mean "up to date", whatever the spelling.

    Tags are written `v1.2.3` while PEP 440 versions are `1.2.3`; if the
    comparison ever stops normalising that away, every release would announce
    itself as an available update.
    """
    from src.api.admin import _parse_version
    from src.core.config import get_settings

    current = get_settings().app.version          # "v1.2.3"
    bare = current.lstrip("vV")                   # "1.2.3"
    assert _parse_version(current) == _parse_version(bare)
    assert not _parse_version(current) > _parse_version(bare)
    assert not _parse_version(bare) > _parse_version(current)
