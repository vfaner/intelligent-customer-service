"""Tests for the multilingual layer (zh-CN · zh-TW · ja · en).

The invariants that actually matter here:

  * A bad or missing ``lang`` must never break a chat request — it degrades to
    Simplified Chinese, so clients written before this feature keep working.
  * Strings the backend returns *without* an LLM round-trip (the canned refusal)
    must be real translations, not a prompt instruction.
  * The zh-CN path must stay byte-identical to the pre-i18n prompt. The comment
    on RAG_SYSTEM_PROMPT warns that small wording changes flip refusal
    behaviour, so "we added i18n" must not mean "we retuned Chinese".
"""
from __future__ import annotations

import pytest

from src.core.i18n import (
    AGENT_NAME,
    DEFAULT_LANG,
    ERROR_PREFIX,
    NEW_SESSION_TITLE,
    STREAM_ERROR_INTERNAL,
    SUPPORTED_LANGS,
    assert_complete,
    normalize_lang,
    pick,
)
from src.prompts import (
    FALLBACK_SYSTEM_PROMPT,
    NO_HIT_REPLY,
    OUTPUT_LANG_RULE,
    RAG_SYSTEM_PROMPT,
    build_rag_user_prompt,
    output_lang_rule,
)


# ----- normalize_lang -------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        # Canonical values pass through.
        ("zh-CN", "zh-CN"), ("zh-TW", "zh-TW"), ("ja", "ja"), ("en", "en"),
        # Case and separator tolerance.
        ("zh_TW", "zh-TW"), ("ZH-tw", "zh-TW"), ("JA", "ja"), ("EN", "en"),
        # Script / region subtags decide simplified vs traditional.
        ("zh", "zh-CN"), ("zh-Hans", "zh-CN"), ("zh-SG", "zh-CN"), ("zh-MY", "zh-CN"),
        ("zh-Hant", "zh-TW"), ("zh-Hant-HK", "zh-TW"), ("zh-MO", "zh-TW"),
        # Legacy Microsoft codes.
        ("zh-CHS", "zh-CN"), ("zh-CHT", "zh-TW"),
        # Cantonese: traditional unless explicitly simplified.
        ("yue", "zh-TW"), ("yue-Hant-HK", "zh-TW"), ("yue-Hans", "zh-CN"),
        # Regional variants of the plain languages.
        ("ja-JP", "ja"), ("en-US", "en"), ("en-GB", "en"),
    ],
)
def test_normalize_lang_maps_tags(raw: str, expected: str) -> None:
    assert normalize_lang(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "fr", "de-DE", "bogus", "!!", "zz-ZZ"])
def test_normalize_lang_degrades_instead_of_raising(raw) -> None:
    """An unusable tag must fall back, never raise — it arrives from the wire."""
    assert normalize_lang(raw) == DEFAULT_LANG


def test_default_is_simplified_chinese_for_backwards_compatibility() -> None:
    # Clients that predate the `lang` field send nothing at all; they must keep
    # getting exactly what they got before.
    assert DEFAULT_LANG == "zh-CN"


# ----- translation table completeness ---------------------------------------
@pytest.mark.parametrize(
    "table,name",
    [
        (NO_HIT_REPLY, "NO_HIT_REPLY"),
        (OUTPUT_LANG_RULE, "OUTPUT_LANG_RULE"),
        (AGENT_NAME, "AGENT_NAME"),
        (NEW_SESSION_TITLE, "NEW_SESSION_TITLE"),
        (ERROR_PREFIX, "ERROR_PREFIX"),
        (STREAM_ERROR_INTERNAL, "STREAM_ERROR_INTERNAL"),
    ],
)
def test_every_table_covers_every_language(table: dict, name: str) -> None:
    assert_complete(table, name)


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_no_hit_reply_is_a_real_translation(lang: str) -> None:
    """This text is returned verbatim without an LLM call, so it must be
    translated rather than left as a prompt instruction."""
    reply = NO_HIT_REPLY[lang]
    assert reply.strip(), f"{lang} refusal is empty"
    if lang != "zh-CN":
        assert reply != NO_HIT_REPLY["zh-CN"], f"{lang} refusal is still the Chinese text"


def test_no_hit_reply_translations_are_distinct() -> None:
    # Simplified and Traditional differ only in a few characters, but they must
    # differ — otherwise a copy-paste slip goes unnoticed.
    assert len(set(NO_HIT_REPLY.values())) == len(SUPPORTED_LANGS)


# ----- prompt assembly ------------------------------------------------------
def test_zh_cn_output_rule_is_a_noop() -> None:
    """The base prompt is already Simplified Chinese; appending anything would
    change the tuned Chinese behaviour for no benefit."""
    assert output_lang_rule("zh-CN") == ""
    assert output_lang_rule(None) == ""


@pytest.mark.parametrize(
    "lang,marker",
    [("zh-TW", "繁體中文"), ("ja", "日本語"), ("en", "English")],
)
def test_non_chinese_output_rule_names_its_language(lang: str, marker: str) -> None:
    """The directive is written in its own target language — an English
    'answer in Japanese' line holds the output language less reliably."""
    rule = output_lang_rule(lang)
    assert marker in rule, f"{lang} rule never names the language"


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_system_prompts_still_format_with_localized_refusal(lang: str) -> None:
    """The language rule is appended after .format(); braces inside it must not
    be able to collide with the prompt's placeholders."""
    system = RAG_SYSTEM_PROMPT.format(
        agent_name=pick(AGENT_NAME, lang),
        refusal_reply=pick(NO_HIT_REPLY, lang),
    ) + output_lang_rule(lang)
    assert pick(AGENT_NAME, lang) in system
    assert pick(NO_HIT_REPLY, lang) in system

    fallback = FALLBACK_SYSTEM_PROMPT.format(agent_name=pick(AGENT_NAME, lang))
    fallback += output_lang_rule(lang)
    assert pick(AGENT_NAME, lang) in fallback


@pytest.mark.parametrize("lang", SUPPORTED_LANGS)
def test_user_prompt_carries_the_question_and_context(lang: str) -> None:
    prompt = build_rag_user_prompt("価格は？", ["[1] 价格是 99 元"], lang=lang)
    assert "価格は？" in prompt
    assert "99 元" in prompt
    assert pick(NO_HIT_REPLY, lang) in prompt


def test_user_prompt_ends_with_language_reminder_for_non_chinese() -> None:
    """Instructions closest to the end of the prompt are followed most
    reliably, and output language is the one rule we can't afford to lose."""
    for lang, marker in [("zh-TW", "繁體中文"), ("ja", "日本語"), ("en", "English")]:
        tail = build_rag_user_prompt("q", ["[1] ctx"], lang=lang)[-60:]
        assert marker in tail, f"{lang} reminder is not at the end of the prompt"


def test_zh_cn_user_prompt_is_unchanged() -> None:
    """Regression guard: adding i18n must not retune the Chinese prompt."""
    prompt = build_rag_user_prompt("多少钱", ["[1] 价格"], lang="zh-CN")
    assert prompt.endswith("不要输出 [1]、[2] 这类资料编号，也不要用 # 标题。")
    # And an absent lang must produce the identical string.
    assert build_rag_user_prompt("多少钱", ["[1] 价格"]) == prompt


def test_context_budget_trimming_still_applies_with_lang() -> None:
    """max_chars must be honoured regardless of the language suffix."""
    blocks = [f"[{i}] " + "填充" * 400 for i in range(1, 6)]
    prompt = build_rag_user_prompt("q", blocks, max_chars=600, lang="ja")
    assert "[5]" not in prompt        # trailing chunks dropped
    assert "日本語" in prompt          # language reminder survives trimming


# ----- pick() ---------------------------------------------------------------
def test_pick_falls_back_for_unknown_language() -> None:
    table = {"zh-CN": "中文", "ja": "日本語"}
    assert pick(table, "ja") == "日本語"
    assert pick(table, "en") == "中文"      # not in table → default
    assert pick(table, "garbage") == "中文"
    assert pick(table, None) == "中文"


def test_assert_complete_flags_a_missing_language() -> None:
    with pytest.raises(AssertionError) as err:
        assert_complete({"zh-CN": "x"}, "PARTIAL")
    assert "PARTIAL" in str(err.value)
