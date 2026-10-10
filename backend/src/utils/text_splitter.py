"""Structure-aware text chunking.

The naive character-window splitter cuts mid-section, which hurts retrieval
two ways:

  * A chunk can span three unrelated sections, so its embedding is a blurry
    average and matches everything weakly.
  * A chunk can start mid-answer, losing the heading that says what it's
    about ("Q4: is my data safe?" with no clue it's under 常见问题).

``split_text`` therefore first breaks the document into *blocks* along
structural boundaries it can recognise — CJK bracket headings (【…】),
Markdown headings, numbered/《》 chapter titles, Q&A pairs, list runs, and
sheet markers emitted by the xlsx parser — then packs those blocks into
chunks without crossing a heading, and prefixes each chunk with the heading
path it belongs to.

Set ``strategy="window"`` to get the old behaviour.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Literal, Optional, Sequence

log = logging.getLogger(__name__)

Strategy = Literal["structure", "window"]


@dataclass
class TextChunk:
    text: str
    index: int
    start: int
    end: int
    heading: str = ""          # heading path this chunk sits under, if any


# ---------------------------------------------------------------------------
# Heading / boundary patterns
# ---------------------------------------------------------------------------
# Ordered by specificity — the first match wins.
_HEADING_PATTERNS: Sequence[tuple[str, "re.Pattern[str]", int]] = (
    # ("kind", pattern, level)
    ("md",       re.compile(r"^(#{1,6})\s+(.+?)\s*$"), 0),          # level from # count
    ("bracket",  re.compile(r"^\s*【\s*(.+?)\s*】\s*$"), 2),          # 【保修条款】
    ("bracket2", re.compile(r"^\s*[\[［]\s*(.+?)\s*[\]］]\s*$"), 2),   # [保修条款]
    ("chapter",  re.compile(r"^\s*第[一二三四五六七八九十百零\d]+[章节篇部分条]\s*[、.：:]?\s*(.*)$"), 1),
    ("sheet",    re.compile(r"^##\s*Sheet:\s*(.+?)\s*$"), 1),        # from xlsx parser
    ("page",     re.compile(r"^##\s*Page\s+(\d+)\s*$"), 1),          # from pdf parser
    ("numbered", re.compile(r"^\s*(\d+(?:\.\d+)*)[、.]\s*(\S.{0,40})$"), 3),
    ("underline",re.compile(r"^\s*([^\n]{1,40})\s*[:：]\s*$"), 3),    # 标题：
)

# A Q&A turn: Q1: / Q： / 问： / Q. …
_QA_START = re.compile(r"^\s*(?:Q\s*\d*|问)\s*[:：.、]", re.IGNORECASE)
_QA_ANSWER = re.compile(r"^\s*(?:A\s*\d*|答)\s*[:：.、]", re.IGNORECASE)

# Sentence terminators used when a single block still exceeds chunk_size.
_SENTENCE_ENDS = ("\n\n", "。", "！", "？", "；", "!", "?", ";", ".\n", ". ", "\n")


# Lines that only record where a chunk came from — useful context to keep in
# the text, but they don't make an otherwise-empty section worth indexing.
_METADATA_LINE = re.compile(
    r"""^\s*(?:
          [（(]\s*本节出自\s*[:：].*?[)）]     # （本节出自：https://…）
        | (?:文章|页面|栏目|原文|来源|出处)\s*(?:地址)?\s*[:：]\s*\S*
        | (?:所属栏目|标签|发布日期|更新日期|作者)\s*[:：].*
        | https?://\S+                        # a URL on its own line
        )\s*$""",
    re.VERBOSE,
)


def _is_metadata_line(line: str) -> bool:
    """True when the line carries only provenance, not answerable content."""
    return bool(_METADATA_LINE.match(line))


@dataclass
class _Block:
    """A structurally coherent run of lines."""
    text: str
    start: int
    end: int
    heading: str = ""              # heading path, e.g. "常见问题"
    is_heading: bool = False       # the block *is* a heading line
    atomic: bool = False           # never split further if we can avoid it
    meta: dict = field(default_factory=dict)


def _match_heading(line: str) -> Optional[tuple[str, int]]:
    """Return ``(title, level)`` when ``line`` looks like a heading."""
    stripped = line.strip()
    if not stripped or len(stripped) > 80:
        return None
    for kind, pat, level in _HEADING_PATTERNS:
        m = pat.match(line)
        if not m:
            continue
        if kind == "md":
            hashes, title = m.group(1), m.group(2)
            return title.strip(), len(hashes)
        if kind == "sheet":
            return f"Sheet: {m.group(1).strip()}", level
        if kind == "page":
            return f"Page {m.group(1)}", level
        if kind == "chapter":
            return stripped.rstrip("：:、."), level
        if kind == "numbered":
            # Only treat as a heading when the line is short and has no
            # sentence terminator — otherwise "1. 整机保修期为 12 个月。" would
            # be misread as a heading rather than content.
            title = m.group(2).strip()
            if any(ch in title for ch in "。！？；"):
                return None
            return title, level
        if kind == "underline":
            title = m.group(1).strip()
            if any(ch in title for ch in "。！？；"):
                return None
            return title, level
        # bracket / bracket2
        return m.group(1).strip(), level
    return None


def _segment_blocks(text: str) -> List[_Block]:
    """Split ``text`` into heading-aware blocks, tracking the heading path."""
    lines = text.splitlines(keepends=True)
    blocks: List[_Block] = []

    # heading stack: list of (level, title)
    stack: List[tuple[int, str]] = []
    buf: List[str] = []
    buf_start = 0
    offset = 0

    def heading_path() -> str:
        return " > ".join(t for _, t in stack)

    def flush(end: int, *, atomic: bool = False) -> None:
        nonlocal buf, buf_start
        if not buf:
            return
        raw = "".join(buf)
        if raw.strip():
            blocks.append(
                _Block(text=raw.strip(), start=buf_start, end=end,
                       heading=heading_path(), atomic=atomic)
            )
        buf = []
        buf_start = end

    for line in lines:
        line_start = offset
        offset += len(line)

        hit = _match_heading(line)
        if hit:
            title, level = hit
            flush(line_start)
            # Pop deeper-or-equal levels, then push this one.
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            blocks.append(
                _Block(text=line.strip(), start=line_start, end=offset,
                       heading=heading_path(), is_heading=True, atomic=True)
            )
            buf_start = offset
            continue

        # A new Q: starts a new atomic block (keep Q and its A together).
        if _QA_START.match(line) and buf:
            flush(line_start)
            buf_start = line_start

        buf.append(line)

        # An answer line ends the Q&A block once we hit a blank line.
        if not line.strip() and buf and _QA_START.match(buf[0] or ""):
            flush(offset, atomic=True)
            buf_start = offset

    flush(offset)
    return blocks


def _split_long_text(text: str, start: int, chunk_size: int, chunk_overlap: int) -> List[tuple[str, int, int]]:
    """Window-split a single oversized block on sentence boundaries."""
    out: List[tuple[str, int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        end = min(i + chunk_size, n)
        if end < n:
            for sep in _SENTENCE_ENDS:
                pos = text.rfind(sep, i + chunk_size // 2, end)
                if pos != -1 and pos + len(sep) <= end:
                    end = pos + len(sep)
                    break
        piece = text[i:end].strip()
        if piece:
            out.append((piece, start + i, start + end))
        if end == n:
            break
        # Advance from the ACTUAL end (which sentence-boundary search may have
        # pulled far back), not from a fixed i + (chunk_size - overlap) step.
        # The old fixed step left [end, i+step) in no chunk at all — text was
        # silently lost at ingest time. `i + 1` keeps progress when overlap=0.
        i = max(end - chunk_overlap, i + 1)
    return out


def _structure_split(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
    *,
    prefix_heading: bool = True,
) -> List[TextChunk]:
    blocks = _segment_blocks(text)
    if not blocks:
        return []

    chunks: List[TextChunk] = []
    idx = 0

    # Pack consecutive blocks that share a heading, flushing on heading change.
    cur: List[_Block] = []
    cur_len = 0

    def cur_heading() -> str:
        for b in cur:
            if b.heading:
                return b.heading
        return ""

    def emit() -> None:
        nonlocal cur, cur_len, idx
        if not cur:
            return
        body = "\n".join(b.text for b in cur).strip()
        if body:
            heading = cur_heading()
            # Prefix the heading path so a retrieved fragment carries its
            # context, unless the block already starts with that heading.
            out = body
            if prefix_heading and heading and not body.startswith(heading):
                first = heading.split(" > ")[-1]
                if first not in body.split("\n", 1)[0]:
                    out = f"[{heading}]\n{body}"
            chunks.append(
                TextChunk(text=out, index=idx, start=cur[0].start,
                          end=cur[-1].end, heading=heading)
            )
            idx += 1
        cur = []
        cur_len = 0

    for b in blocks:
        # A heading always begins a new chunk.
        if b.is_heading:
            emit()
            cur = [b]
            cur_len = len(b.text)
            continue

        # An oversized block gets window-split on its own. Carry any pending
        # heading into the first piece so it isn't left as a content-free chunk.
        if len(b.text) > chunk_size:
            pending_heading_line = ""
            if cur and all(x.is_heading for x in cur):
                pending_heading_line = "\n".join(x.text for x in cur)
                cur = []
                cur_len = 0
            else:
                emit()

            heading = b.heading
            pieces = _split_long_text(b.text, b.start, chunk_size, chunk_overlap)
            for n_i, (piece, s, e) in enumerate(pieces):
                out = piece
                if n_i == 0 and pending_heading_line:
                    out = f"{pending_heading_line}\n{piece}"
                elif prefix_heading and heading:
                    first = heading.split(" > ")[-1]
                    if first not in piece.split("\n", 1)[0]:
                        out = f"[{heading}]\n{piece}"
                chunks.append(TextChunk(text=out, index=idx, start=s, end=e, heading=heading))
                idx += 1
            continue

        # Would adding this block overflow? Flush first.
        if cur and cur_len + len(b.text) + 1 > chunk_size:
            emit()
        cur.append(b)
        cur_len += len(b.text) + 1

    emit()

    # Drop chunks that carry a heading but no body — they'd match queries
    # strongly (the heading echoes the question) while answering nothing.
    #
    # "Body" excludes provenance/metadata lines a knowledge base may attach to
    # every section (source URLs, 「文章地址：」, 「本节出自：」…). Without this,
    # a bare "#### 项目介绍" plus a source line counts as content and outranks
    # the section that actually answers the question — measured at 18% of one
    # real corpus.
    kept: List[TextChunk] = []
    for c in chunks:
        body = c.text
        if c.heading and body.startswith(f"[{c.heading}]"):
            body = body[len(c.heading) + 3:]
        lines = [ln for ln in body.splitlines() if ln.strip()]
        if lines and _match_heading(lines[0]):
            lines = lines[1:]
        # Keep only lines that say something beyond where the text came from.
        substantive = [ln for ln in lines if not _is_metadata_line(ln)]
        if not substantive:
            continue
        kept.append(c)

    for i, c in enumerate(kept):
        c.index = i
    return kept


def _fallback_split(text: str, chunk_size: int, chunk_overlap: int) -> List[TextChunk]:
    """Plain sliding-window splitter (previous default, kept as a fallback)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be in [0, chunk_size)")

    text = text.strip()
    if not text:
        return []

    chunks: List[TextChunk] = []
    n = len(text)
    i = 0
    idx = 0
    while i < n:
        end = min(i + chunk_size, n)
        if end < n:
            for sep in _SENTENCE_ENDS:
                pos = text.rfind(sep, i + chunk_size // 2, end)
                if pos != -1 and pos + len(sep) <= end:
                    end = pos + len(sep)
                    break
        piece = text[i:end].strip()
        if piece:
            chunks.append(TextChunk(text=piece, index=idx, start=i, end=end))
            idx += 1
        if end == n:
            break
        # Anchor the next window to the actual `end` (see _split_long_text):
        # a fixed i += chunk_size - overlap step silently dropped whatever the
        # sentence-boundary search had skipped over.
        i = max(end - chunk_overlap, i + 1)
    return chunks


def split_text(
    text: str,
    chunk_size: int = 500,
    chunk_overlap: int = 100,
    separators: Optional[List[str]] = None,
    *,
    strategy: Strategy = "structure",
    prefix_heading: bool = True,
) -> List[TextChunk]:
    """Split ``text`` into retrieval-friendly chunks.

    ``strategy="structure"`` (default) respects headings, Q&A pairs and lists,
    and prefixes each chunk with its heading path. ``strategy="window"`` is the
    old fixed-size sliding window.
    """
    if not text or not text.strip():
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be > 0")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be in [0, chunk_size)")

    if strategy == "structure":
        try:
            chunks = _structure_split(
                text, chunk_size, chunk_overlap, prefix_heading=prefix_heading
            )
            if chunks:
                return chunks
        except Exception:
            # Never let a splitter bug block ingestion — but make it visible:
            # silently falling back hides splitter regressions.
            log.warning("structure split failed; falling back to window split", exc_info=True)
    return _fallback_split(text, chunk_size, chunk_overlap)
