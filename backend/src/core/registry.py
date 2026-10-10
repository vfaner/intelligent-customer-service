"""Static registries: supported AI providers, vector DBs, parsers.

Provider list extends the well-known OpenAI / Anthropic ecosystem with
Chinese vendors (Qwen, Bailian, Doubao, Volcengine ARK, ERNIE/Qianfan,
GLM/Zhipu, Kimi/Moonshot) plus gateway providers (Shengsuanyun,
Youyunjisuan). Each entry carries the base URL and a sensible default
model so the front-end can pre-fill the form when a vendor is selected.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Literal, Optional, Tuple

Protocol = Literal["openai", "anthropic"]


# ---------------------------------------------------------------------------
# AI Providers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProtocolEndpoint:
    """One protocol a vendor speaks, with the base URL to use for it."""
    protocol: Protocol
    base_url: str
    default_model: str
    label: str = ""            # optional override for the protocol dropdown

    def to_dict(self) -> Dict[str, object]:
        return {
            "protocol": self.protocol,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "label": self.label,
        }


@dataclass(frozen=True)
class ProviderSpec:
    """A vendor. One entry per vendor — never per (vendor, protocol) pair.

    Vendors that expose several protocols (e.g. Volcengine ARK speaks both
    the OpenAI and the Anthropic schema) list them all in ``endpoints``.
    The UI shows the vendor once and lets the user switch protocol, which
    swaps the base URL and default model accordingly.
    """
    code: str
    display_name: str
    endpoints: Tuple[ProtocolEndpoint, ...]
    default_embedding_model: str = ""
    recommended_models: Tuple[str, ...] = ()   # fallback when the API can't be listed
    supports_model_listing: bool = True
    notes: str = ""
    is_custom: bool = False
    verified: bool = True                      # False → UI warns to double-check
    aliases: Tuple[str, ...] = ()              # old codes that map here

    # ----- convenience accessors (default = first endpoint) -----
    @property
    def protocol(self) -> Protocol:
        return self.endpoints[0].protocol

    @property
    def base_url(self) -> str:
        return self.endpoints[0].base_url

    @property
    def default_model(self) -> str:
        return self.endpoints[0].default_model

    @property
    def protocols(self) -> Tuple[Protocol, ...]:
        return tuple(e.protocol for e in self.endpoints)

    def endpoint_for(self, protocol: str) -> Optional[ProtocolEndpoint]:
        for e in self.endpoints:
            if e.protocol == protocol:
                return e
        return None

    def to_dict(self) -> Dict[str, object]:
        return {
            "code": self.code,
            "display_name": self.display_name,
            "endpoints": [e.to_dict() for e in self.endpoints],
            "protocols": list(self.protocols),
            # Flat fields kept for backwards compatibility with older callers.
            "protocol": self.protocol,
            "base_url": self.base_url,
            "default_model": self.default_model,
            "default_embedding_model": self.default_embedding_model,
            "recommended_models": list(self.recommended_models),
            "supports_model_listing": self.supports_model_listing,
            "notes": self.notes,
            "is_custom": self.is_custom,
            "verified": self.verified,
        }


# Curated registry.
#
# ⚠ Maintenance rule: every base_url / default_model below is either
#   (a) carried over from a configuration the user has actually used, or
#   (b) marked `verified=False` so the UI can warn before someone trusts it.
# Do NOT add endpoints from memory without marking them unverified — a wrong
# ARK base URL silently moved billing from a subscription to pay-as-you-go.
AI_PROVIDERS: List[ProviderSpec] = [
    ProviderSpec(
        code="deepseek",
        display_name="DeepSeek（深度求索）",
        endpoints=(
            ProtocolEndpoint("openai", "https://api.deepseek.com/v1", "deepseek-chat"),
        ),
        recommended_models=("deepseek-chat", "deepseek-reasoner"),
        notes="性价比高；如需 Anthropic 协议请用「自定义（Anthropic 协议）」自行填写地址",
    ),
    ProviderSpec(
        code="dashscope",
        display_name="阿里云 · 通义千问 / 百炼",
        endpoints=(
            ProtocolEndpoint(
                "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"
            ),
        ),
        default_embedding_model="text-embedding-v3",
        recommended_models=("qwen-max", "qwen-plus", "qwen-turbo", "qwen-long"),
        notes="千问与百炼共用 DashScope 兼容模式地址，已合并为一项",
        aliases=("qwen", "bailian"),
    ),
    ProviderSpec(
        code="volcengine_plan",
        display_name="火山方舟 · 包月套餐（Coding Plan）",
        endpoints=(
            ProtocolEndpoint(
                "openai", "https://ark.cn-beijing.volces.com/api/plan/v3", "ark-code-latest"
            ),
            ProtocolEndpoint(
                "anthropic", "https://ark.cn-beijing.volces.com/api/plan/v1", "ark-code-latest"
            ),
        ),
        recommended_models=("ark-code-latest",),
        supports_model_listing=False,
        notes=(
            "包月套餐专用地址（/api/plan）。模型固定填 ark-code-latest，"
            "路由到哪个具体模型（auto / doubao-seed-evolving / …）在火山控制台选。"
            "⚠ 不要改成 /api/v3 —— 那是按量付费，会另外计费"
        ),
        aliases=("volcengine",),
    ),
    ProviderSpec(
        code="doubao",
        display_name="火山方舟 · 豆包（按量付费）",
        endpoints=(
            ProtocolEndpoint(
                "openai", "https://ark.cn-beijing.volces.com/api/v3", "doubao-pro-32k"
            ),
        ),
        recommended_models=("doubao-pro-32k", "doubao-pro-256k", "doubao-lite-32k"),
        notes="按量付费地址（/api/v3）。买了包月套餐请改选上面的「包月套餐」项",
        aliases=("volcengine_ark", "volcengine_anthropic"),
    ),
    ProviderSpec(
        code="zhipu",
        display_name="智谱 GLM",
        endpoints=(
            ProtocolEndpoint("openai", "https://open.bigmodel.cn/api/paas/v4", "glm-4-plus"),
        ),
        recommended_models=("glm-4-plus", "glm-4-air", "glm-4-flash"),
        notes="智谱 AI 开放平台",
    ),
    ProviderSpec(
        code="kimi",
        display_name="Kimi（Moonshot）",
        endpoints=(
            ProtocolEndpoint("openai", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
        ),
        recommended_models=("moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"),
        notes="月之暗面；长上下文见 32k / 128k 型号",
    ),
    ProviderSpec(
        code="qianfan",
        display_name="百度千帆（ERNIE）",
        endpoints=(
            ProtocolEndpoint("openai", "https://qianfan.baidubce.com/v2", "ernie-4.0-turbo-8k"),
        ),
        recommended_models=("ernie-4.0-turbo-8k", "ernie-4.0-8k", "ernie-3.5-8k"),
        notes="百度智能云千帆 v2 端点",
    ),
    ProviderSpec(
        code="openai",
        display_name="OpenAI",
        endpoints=(ProtocolEndpoint("openai", "https://api.openai.com/v1", "gpt-4o-mini"),),
        default_embedding_model="text-embedding-3-small",
        recommended_models=("gpt-4o", "gpt-4o-mini"),
        notes="OpenAI 官方，国内需自备代理。点 🔄 可拉取你账号可用的完整模型列表",
    ),
    ProviderSpec(
        code="anthropic",
        display_name="Anthropic Claude",
        endpoints=(
            ProtocolEndpoint(
                "anthropic", "https://api.anthropic.com/v1", "claude-3-5-sonnet-latest"
            ),
        ),
        recommended_models=("claude-3-5-sonnet-latest", "claude-3-5-haiku-latest"),
        notes="Anthropic 官方 Messages 协议。新模型名请查官网，或点 🔄 拉取",
    ),
    ProviderSpec(
        code="shengsuanyun",
        display_name="胜算云（模型路由）",
        endpoints=(
            ProtocolEndpoint("openai", "https://router.shengsuanyun.com/api/v1", "deepseek-chat"),
        ),
        recommended_models=("deepseek-chat",),
        notes="聚合多家模型的路由服务；可用模型以其控制台为准，建议点 🔄 拉取",
    ),
    ProviderSpec(
        code="youyunjisuan",
        display_name="优云智算",
        endpoints=(
            ProtocolEndpoint("openai", "https://api.modelverse.cn/v1", "deepseek-chat"),
        ),
        recommended_models=("deepseek-chat",),
        notes="优云智算 ModelVerse；建议点 🔄 拉取可用模型",
        verified=False,
    ),
    ProviderSpec(
        code="gemini",
        display_name="Google Gemini",
        endpoints=(
            ProtocolEndpoint(
                "openai",
                "https://generativelanguage.googleapis.com/v1beta/openai",
                "gemini-1.5-flash",
            ),
        ),
        default_embedding_model="text-embedding-004",
        recommended_models=("gemini-1.5-pro", "gemini-1.5-flash"),
        notes="通过 OpenAI 兼容端点接入；新模型名请点 🔄 拉取确认",
        verified=False,
    ),
    ProviderSpec(
        code="ollama",
        display_name="Ollama（本地部署）",
        endpoints=(
            ProtocolEndpoint("openai", "http://localhost:11434/v1", "qwen2.5:14b"),
        ),
        default_embedding_model="nomic-embed-text",
        recommended_models=("qwen2.5:14b", "qwen2.5:7b", "llama3.2"),
        notes="本地跑模型，API Key 随便填（如 ollama）。点 🔄 会列出你已 pull 的模型",
        verified=False,
    ),
    ProviderSpec(
        code="openai_custom",
        display_name="自定义（OpenAI 协议）",
        endpoints=(ProtocolEndpoint("openai", "", ""),),
        notes="任何兼容 OpenAI 接口的服务：vLLM / one-api / LiteLLM / 自建网关等",
        is_custom=True,
    ),
    ProviderSpec(
        code="anthropic_custom",
        display_name="自定义（Anthropic 协议）",
        endpoints=(ProtocolEndpoint("anthropic", "", ""),),
        supports_model_listing=False,
        notes="任何兼容 Anthropic Messages 接口的服务（含各厂商的 /anthropic 兼容层）",
        is_custom=True,
    ),
]

AI_PROVIDERS_BY_CODE: Dict[str, ProviderSpec] = {p.code: p for p in AI_PROVIDERS}

# Old provider codes → merged entries, so saved configs keep working.
PROVIDER_ALIASES: Dict[str, str] = {
    alias: p.code for p in AI_PROVIDERS for alias in p.aliases
}


def list_providers() -> List[Dict[str, object]]:
    return [p.to_dict() for p in AI_PROVIDERS]


def get_provider(code: str) -> Optional[ProviderSpec]:
    """Look up a provider, transparently following renamed/merged codes."""
    if not code:
        return None
    if code in AI_PROVIDERS_BY_CODE:
        return AI_PROVIDERS_BY_CODE[code]
    merged = PROVIDER_ALIASES.get(code)
    return AI_PROVIDERS_BY_CODE.get(merged) if merged else None


def resolve_provider_code(code: str) -> str:
    """Map a possibly-legacy code onto the current one."""
    spec = get_provider(code)
    return spec.code if spec else code


# ---------------------------------------------------------------------------
# Vector DB providers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VectorDBProvider:
    code: str
    display_name: str
    notes: str = ""


VECTOR_DB_PROVIDERS: List[VectorDBProvider] = [
    VectorDBProvider("chroma", "Chroma",  "轻量级本地向量库，适合开发与中小规模"),
    VectorDBProvider("qdrant", "Qdrant",  "Rust 实现的高性能向量库，适合生产"),
    VectorDBProvider("milvus", "Milvus",  "企业级向量库；本地用 Milvus Lite（.db 文件），生产连 standalone/集群或 Zilliz Cloud"),
]

def list_vector_db_providers() -> List[Dict[str, str]]:
    return [{"code": p.code, "display_name": p.display_name, "notes": p.notes}
            for p in VECTOR_DB_PROVIDERS]


# ---------------------------------------------------------------------------
# Document parsers
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DocumentParserSpec:
    code: str
    display_name: str
    extensions: Tuple[str, ...]
    notes: str = ""


DOCUMENT_PARSERS: List[DocumentParserSpec] = [
    DocumentParserSpec("txt",  "纯文本 / Markdown", (".txt", ".md", ".markdown"),
                       "UTF-8 文本；Markdown 标题会被切分器识别为章节"),
    DocumentParserSpec("docx", "Word 文档", (".docx",),     "使用 python-docx 解析"),
    DocumentParserSpec("xlsx", "Excel 表格",(".xlsx",),     "使用 openpyxl 解析"),
    DocumentParserSpec("pdf",  "PDF 文档", (".pdf",),       "使用 PyPDF2 解析（可选）"),
]

def parser_for_filename(filename: str) -> Optional[DocumentParserSpec]:
    name = filename.lower()
    for spec in DOCUMENT_PARSERS:
        if name.endswith(spec.extensions):
            return spec
    return None


def list_document_parsers() -> List[Dict[str, object]]:
    return [
        {
            "code": p.code,
            "display_name": p.display_name,
            "extensions": list(p.extensions),
            "notes": p.notes,
        }
        for p in DOCUMENT_PARSERS
    ]
