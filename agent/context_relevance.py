"""Deterministic relevant-context pin selection for compression.

The MVP is intentionally stdlib-only and lexical.  It selects compact,
reference-only excerpts from the compression middle window so the summarizer can
preserve old paths/errors/decisions without reintroducing old messages as live
conversation turns.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, NamedTuple

from agent.redact import redact_sensitive_text


@dataclass(frozen=True)
class RelevantContextPin:
    """A compact reference-only excerpt selected from older context."""

    index: int
    role: str
    score: int
    reason: str
    excerpt: str


class QueryTerms(NamedTuple):
    """Terms extracted from the current task and recent tail."""

    words: frozenset[str]
    exact: frozenset[str]
    phrases: frozenset[str]


_STOP_WORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "what",
        "why",
        "how",
        "was",
        "were",
        "are",
        "from",
        "into",
        "about",
        "uma",
        "com",
        "que",
        "para",
        "por",
        "dos",
        "das",
        "está",
        "esta",
        "esse",
        "isso",
    }
)

_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+")
_ERROR_CODE_RE = re.compile(
    r"(?:\b(?:[A-Z][A-Za-z]+Error|HTTP\s+\d{3}|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]+)\b|#[0-9]{2,})"
)
_QUOTED_PHRASE_RE = re.compile(r"['\"]([^'\"]{3,120})['\"]")
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_-]{2,}")
_ITEM_PHRASE_RE = re.compile(r"\b(?:item|itens?)\s+\d+(?:\s*(?:,|e|and)\s*\d+)*\b", re.IGNORECASE)
_DECISION_MARKER_RE = re.compile(
    r"\b(blocked|blocker|error|failed|failure|decided|decision|remaining|todo|must|root cause|causa raiz|bloqueado|falhou|erro)\b",
    re.IGNORECASE,
)


def _normalise_text(text: str) -> str:
    return " ".join((text or "").split())


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content)


def _extract_query_terms(
    current_text: str,
    *,
    focus_topic: str | None = None,
    tail_text: str | None = None,
) -> QueryTerms:
    """Extract lexical query terms from current task/focus/recent tail."""

    source = "\n".join(part for part in [current_text or "", focus_topic or "", tail_text or ""] if part)
    exact: set[str] = set()
    phrases: set[str] = set()

    exact.update(_PATH_RE.findall(source))
    error_code_terms = _ERROR_CODE_RE.findall(source)
    exact.update(error_code_terms)
    phrases.update(term for term in error_code_terms if term.upper().startswith("HTTP"))
    phrases.update(match.group(1) for match in _QUOTED_PHRASE_RE.finditer(source))
    for match in _ITEM_PHRASE_RE.finditer(source):
        item_phrase = match.group(0)
        phrases.add(item_phrase)
        for number in re.findall(r"\d+", item_phrase):
            phrases.add(f"item {number}")

    words: set[str] = set()
    for token in _WORD_RE.findall(source.lower()):
        if token in _STOP_WORDS:
            continue
        # Path/error tokens are already represented exactly; keep their parts
        # too because users often refer to a file by basename later.
        words.add(token)

    return QueryTerms(frozenset(words), frozenset(exact), frozenset(phrases))


def _latest_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return _message_text(message)
    return ""


def _tail_text(messages: list[dict[str, Any]], candidate_end: int, max_messages: int = 8) -> str:
    tail = messages[candidate_end:]
    if len(tail) > max_messages:
        tail = tail[-max_messages:]
    return "\n".join(_message_text(message) for message in tail)


def _is_context_summary(text: str) -> bool:
    stripped = text.lstrip()
    return (
        stripped.startswith("[CONTEXT COMPACTION")
        or stripped.startswith("[CONTEXT SUMMARY]:")
    )


def _format_excerpt(message: dict[str, Any], *, max_chars: int = 1200) -> str:
    if max_chars <= 0:
        return ""
    text = redact_sensitive_text(_normalise_text(_message_text(message)))
    if len(text) <= max_chars:
        return text
    if max_chars <= 15:
        return text[:max_chars]
    return text[: max_chars - 15].rstrip() + " ...[truncated]"


def _score_message(message: dict[str, Any], terms: QueryTerms, relative_recency: int) -> tuple[int, list[str]]:
    text = _message_text(message)
    if not text or _is_context_summary(text):
        return -100, ["context-summary"]

    lower = text.lower()
    score = 0
    reasons: list[str] = []

    exact_hits = 0
    for token in terms.exact:
        if token and token.lower() in lower:
            exact_hits += 1
    if exact_hits:
        score += exact_hits * 3
        reasons.append("exact")
        if any("/" in token for token in terms.exact if token.lower() in lower):
            reasons.append("path")
        if any("error" in token.lower() or token.upper().startswith("HTTP") or "_" in token for token in terms.exact if token.lower() in lower):
            reasons.append("error")

    phrase_hits = sum(1 for phrase in terms.phrases if phrase and phrase.lower() in lower)
    if phrase_hits:
        # Compact references such as "itens 1 e 2" and quoted phrases are
        # deliberate user handles, closer to exact matches than loose words.
        score += phrase_hits * 3
        reasons.append("phrase")

    word_hits = sum(1 for word in terms.words if word and word in lower)
    if word_hits:
        score += word_hits
        reasons.append("word")

    if message.get("role") == "user" and _DECISION_MARKER_RE.search(text):
        score += 2
        reasons.append("user-decision")
    elif _DECISION_MARKER_RE.search(text):
        score += 2
        reasons.append("marker")

    if relative_recency <= 3:
        score += 1
        reasons.append("recent-middle")

    return score, reasons


def select_relevant_context_pins(
    messages: list[dict[str, Any]],
    *,
    candidate_start: int,
    candidate_end: int,
    focus_topic: str | None = None,
    max_pins: int = 8,
    max_chars_total: int = 12000,
    min_score: int = 3,
) -> list[RelevantContextPin]:
    """Select compact relevant excerpts from the compression middle window.

    Tool results are deliberately not emitted as standalone pins; preserving raw
    old tool output as active-looking context is unsafe and can break the mental
    model of tool-call/result pairing.  Assistant tool-call messages may still be
    summarized as ordinary assistant excerpts.
    """

    if max_pins <= 0 or max_chars_total <= 0 or candidate_start >= candidate_end:
        return []

    candidate_start = max(0, candidate_start)
    candidate_end = min(len(messages), candidate_end)
    terms = _extract_query_terms(
        _latest_user_text(messages),
        focus_topic=focus_topic,
        tail_text=_tail_text(messages, candidate_end),
    )
    if not (terms.words or terms.exact or terms.phrases):
        return []

    scored: list[tuple[int, int, dict[str, Any], list[str]]] = []
    for idx in range(candidate_start, candidate_end):
        message = messages[idx]
        role = message.get("role", "")
        if role in {"system", "tool"}:
            continue
        relative_recency = candidate_end - idx
        score, reasons = _score_message(message, terms, relative_recency)
        if score >= min_score:
            scored.append((score, idx, message, reasons))

    scored.sort(key=lambda item: (-item[0], item[1]))

    pins: list[RelevantContextPin] = []
    remaining = max_chars_total
    for score, idx, message, reasons in scored[:max_pins]:
        if remaining <= 0:
            break
        excerpt = _format_excerpt(message, max_chars=min(1200, remaining))
        if not excerpt:
            continue
        pins.append(
            RelevantContextPin(
                index=idx,
                role=str(message.get("role", "")),
                score=score,
                reason="+".join(dict.fromkeys(reasons)) or "match",
                excerpt=excerpt,
            )
        )
        remaining -= len(excerpt)

    return pins
