"""Supported languages and the localized strings the backend returns directly.

Scope: this module owns only the text the **backend** produces. UI strings live
in the frontend catalogs (``frontend/admin/i18n.js`` and the table inlined in
``customer-service.js``).

Language detection itself is deliberately client-side only — the offline/intranet
target means IP geolocation is useless (a 10.x address carries no region) and no
online geo API is reachable. The browser resolves its locale from system timezone
+ ``navigator.languages`` and sends the result as ``lang`` on each chat request.
See ``frontend/shared/locale.js``.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional

#: Wire values accepted on the ``lang`` field. Keep in sync with
#: ``LOCALES`` in ``frontend/shared/locale.js``.
SUPPORTED_LANGS = ("zh-CN", "zh-TW", "ja", "en")

#: Requests without a usable ``lang`` fall back to Simplified Chinese so
#: clients written before this feature existed behave exactly as before.
DEFAULT_LANG = "zh-CN"

#: Tolerated spellings for the same locale (case-insensitive, ``_`` == ``-``).
#: Anything not listed here is resolved by the prefix rules in ``normalize_lang``.
_ALIASES: Dict[str, str] = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "zh-hans-cn": "zh-CN",
    "zh-sg": "zh-CN",
    "zh-my": "zh-CN",
    "zh-chs": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-TW",
    "zh-mo": "zh-TW",
    "zh-hant": "zh-TW",
    "zh-hant-tw": "zh-TW",
    "zh-hant-hk": "zh-TW",
    "zh-cht": "zh-TW",
    "ja": "ja",
    "ja-jp": "ja",
    "en": "en",
}

_TRADITIONAL_SUBTAGS = {"hant", "cht", "tw", "hk", "mo"}
_SIMPLIFIED_SUBTAGS = {"hans", "chs", "cn", "sg", "my"}


def normalize_lang(raw: Optional[str]) -> str:
    """Map an arbitrary client-supplied tag onto a supported language.

    Never raises and never returns an unsupported value — a bad ``lang`` must
    degrade to the default rather than break the chat request.
    """
    if not raw:
        return DEFAULT_LANG
    tag = str(raw).strip().replace("_", "-").lower()
    if not tag:
        return DEFAULT_LANG
    if tag in _ALIASES:
        return _ALIASES[tag]

    parts = tag.split("-")
    primary = parts[0]
    if primary == "ja":
        return "ja"
    if primary == "en":
        return "en"
    if primary in ("zh", "yue"):
        subtags = set(parts[1:])
        if subtags & _TRADITIONAL_SUBTAGS:
            return "zh-TW"
        if subtags & _SIMPLIFIED_SUBTAGS:
            return "zh-CN"
        # No script/region hint. Cantonese is overwhelmingly written
        # traditional (yue-Hant-HK); bare `zh` means Simplified.
        return "zh-TW" if primary == "yue" else "zh-CN"
    return DEFAULT_LANG


def pick(table: Dict[str, str], lang: Optional[str]) -> str:
    """Look up ``lang`` in a per-language table, falling back to the default."""
    resolved = normalize_lang(lang)
    if resolved in table:
        return table[resolved]
    return table.get(DEFAULT_LANG, "")


def assert_complete(table: Dict[str, str], name: str) -> None:
    """Raise if a translation table is missing a language (used by tests)."""
    missing: Iterable[str] = [l for l in SUPPORTED_LANGS if l not in table]
    if missing:
        raise AssertionError(f"{name} is missing translations for: {list(missing)}")


# --- Strings the backend emits without going through the LLM ---------------

#: Default agent name, used when the request/admin config doesn't set one.
AGENT_NAME: Dict[str, str] = {
    "zh-CN": "智能客服小助手",
    "zh-TW": "智能客服小助手",
    "ja": "AIカスタマーサポート",
    "en": "AI Support Assistant",
}

#: Title given to an auto-created session when the first message is empty.
NEW_SESSION_TITLE: Dict[str, str] = {
    "zh-CN": "新会话",
    "zh-TW": "新對話",
    "ja": "新しい会話",
    "en": "New conversation",
}

#: Prefix on the assistant turn persisted when streaming blew up.
ERROR_PREFIX: Dict[str, str] = {
    "zh-CN": "[错误]",
    "zh-TW": "[錯誤]",
    "ja": "[エラー]",
    "en": "[Error]",
}

#: Generic stream-failure text for visitors when an unexpected exception
#: aborts an answer. Internal detail goes to the log only — raw exception
#: text can carry paths, config values or vendor URLs, and the persisted
#: history row is replayed to the visitor on reconnect.
STREAM_ERROR_INTERNAL: Dict[str, str] = {
    "zh-CN": "服务暂时不可用，请稍后再试。",
    "zh-TW": "服務暫時不可用，請稍後再試。",
    "ja": "サービスが一時的に利用できません。しばらくしてからもう一度お試しください。",
    "en": "The service is temporarily unavailable. Please try again later.",
}
