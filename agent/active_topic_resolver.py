"""Deterministic active-topic continuity resolver.

This module builds a compact, API-call-time context packet for ambiguous
continuation turns ("continue", "other areas", "pode fazer", etc.).  It is
intentionally deterministic and lightweight: no LLM calls, no prompt-cache
mutation, and no persistence side effects.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from hermes_constants import get_hermes_home

_CONTINUATION_PATTERNS = [
    r"\bcontin(u|ue|ua|uar|uamos|uemos|ue)\b",
    r"\bsegu(e|ir|imos|inte|indo)\b",
    r"\bproxim[oa]s?\s+(passos?|focos?|areas?|frentes?)\b",
    r"\bnext\s+(steps?|focus|areas?)\b",
    r"\bareas?\s+de\s+foco\b",
    r"\bfrentes?\s+(de\s+trabalho|possiveis|novas)?\b",
    r"\boutras?\s+areas?\b",
    r"\bo\s+que\s+(voce|vc)\s+sugere\b",
    r"\bwhat\s+do\s+you\s+suggest\b",
    r"\bpode\s+fazer\b",
    r"\bvamos\b",
    r"\bitem\s*\d+\b",
    r"\bitens\b",
]

_GENERIC_CONTINUATION_WORDS = {
    "continue", "continuar", "continua", "seguir", "seguimos", "proximo",
    "proxima", "proximas", "next", "focus", "foco", "area", "areas",
    "outra", "outras", "sugere", "fazer", "vamos", "item", "itens",
    "com", "mais", "agora", "tambem", "tbm", "voce", "vc", "o", "que",
}

_STOPWORDS = _GENERIC_CONTINUATION_WORDS | {
    "a", "as", "o", "os", "um", "uma", "de", "do", "da", "dos", "das",
    "em", "no", "na", "nos", "nas", "para", "por", "e", "ou", "the", "and",
    "for", "with", "to", "of", "in", "on", "is", "are", "this", "that",
    "isso", "esse", "essa", "aquele", "aquela", "sobre", "ontem",
}

_PROJECT_ROOTS_ENV = "HERMES_ACTIVE_TOPIC_PROJECT_ROOTS"
_MAX_PROJECTS = 80
_MAX_CONTEXT_CHARS = 1800

# Evidence gate: the resolver must observe at least this many topical tokens
# contributed by the *current* user message (not history) before accepting a
# project. Cross-session hint is preserved as a soft signal (+2.0) but cannot
# alone unlock a packet. This is the structural fix for the leak where a
# structurally-typed continuation ("1. Discord / 2. ... / 3. causa raiz")
# picked up a stale project whose tokens lived only in the assistant's prior
# turn.
_MIN_TOPIC_EVIDENCE_TOKENS = 2

# Hint weight reduced from +8.0 (decisive) to +2.0 (supportive). Cross-session
# continuity is preserved by design but no longer dominates the score.
_SESSION_HINT_WEIGHT = 2.0

# Default confidence threshold raised from 0.45 to 0.50. Tunable via
# agent.active_topic.min_confidence in config.yaml. Tunable without code edit.
# 0.50 was chosen empirically against the live vault: it filters the leak
# class (structural continuation with 0.50-0.55 confidence) while leaving
# genuine cross-session continuity (0.60-0.85) untouched.
_DEFAULT_MIN_CONFIDENCE = 0.50


@dataclass(frozen=True)
class ProjectContext:
    slug: str
    title: str
    path: Path
    context_path: Path | None
    text: str
    status: str = ""


@dataclass(frozen=True)
class ActiveTopicPacket:
    topic_label: str
    project_slug: str
    canonical_paths: tuple[str, ...]
    last_artifacts: tuple[str, ...]
    current_open_loop: str
    confidence: float
    why: str
    instructions: tuple[str, ...]

    def format_for_user_message(self) -> str:
        lines = [
            "<active_topic_context>",
            "Purpose: preserve continuity for this ambiguous continuation turn. Use this as local thread/project context, not as a new user request.",
            f"topic_label: {self.topic_label}",
            f"project_slug: {self.project_slug}",
            f"confidence: {self.confidence:.2f}",
            f"why: {self.why}",
        ]
        if self.canonical_paths:
            lines.append("canonical_paths:")
            lines.extend(f"- {p}" for p in self.canonical_paths[:8])
        if self.last_artifacts:
            lines.append("last_artifacts:")
            lines.extend(f"- {p}" for p in self.last_artifacts[:8])
        if self.current_open_loop:
            lines.append(f"current_open_loop: {self.current_open_loop}")
        if self.instructions:
            lines.append("instructions:")
            lines.extend(f"- {i}" for i in self.instructions[:8])
        lines.append("</active_topic_context>")
        return "\n".join(lines)


def normalize_text(text: Any) -> str:
    raw = str(text or "")
    decomposed = unicodedata.normalize("NFKD", raw)
    asciiish = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return asciiish.lower()


def is_continuation_like(message: Any) -> bool:
    text = normalize_text(message)
    if not text.strip():
        return False
    if any(re.search(pattern, text) for pattern in _CONTINUATION_PATTERNS):
        return True
    words = re.findall(r"[a-z0-9_]+", text)
    meaningful = [w for w in words if w not in _STOPWORDS]
    # Short referential messages often rely entirely on prior context.
    return len(words) <= 5 and len(meaningful) <= 2


def _tokenize(text: str) -> set[str]:
    # normalize_text() applies NFKD + strips combining marks so accented
    # Portuguese/European tokens ("negocio", "implementar", "ensinar") match
    # project text that may carry accented forms ("negócio", "implementação").
    # Without this the evidence gate fails on Portuguese project files.
    return {
        w for w in re.findall(r"[a-z0-9_]{3,}", normalize_text(text))
        if w not in _STOPWORDS
    }


def _message_topic_tokens(user_message: Any) -> set[str]:
    """Topical tokens from the *current* user message only.

    Returns meaningful, non-stopword tokens. The evidence gate uses this set
    to enforce that the user actually said something about the resolved
    project's topic in this turn, not just hit a continuation regex.
    """
    if isinstance(user_message, Mapping):
        text = _message_text(user_message)
    else:
        text = str(user_message or "")
    return _tokenize(text)


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return " ".join(parts)
    return ""


def build_seed_text(
    user_message: Any,
    conversation_history: Sequence[Mapping[str, Any]] | None,
    *,
    agent: Any = None,
    max_messages: int = 10,
) -> str:
    parts = [str(user_message or "")]
    if agent is not None:
        for attr in ("_chat_name", "_chat_type", "_thread_id", "_gateway_session_key", "platform", "session_id"):
            value = getattr(agent, attr, None)
            if value:
                parts.append(str(value))
    for msg in list(conversation_history or [])[-max_messages:]:
        if not isinstance(msg, Mapping):
            continue
        if msg.get("role") not in {"user", "assistant"}:
            continue
        text = _message_text(msg)
        if text:
            parts.append(text[:800])
    return "\n".join(parts)


def default_project_roots() -> list[Path]:
    env = os.environ.get(_PROJECT_ROOTS_ENV, "").strip()
    if env:
        return [Path(p).expanduser() for p in env.split(os.pathsep) if p.strip()]
    home = Path.home()
    roots = [
        home / "obsidian-vault" / "Krishna" / "niko" / "research" / "projects",
        get_hermes_home() / "niko" / "research" / "projects",
    ]
    # Preserve order, remove duplicates.
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        if root not in seen:
            out.append(root)
            seen.add(root)
    return out


def _read_text(path: Path, max_chars: int = 8000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[:max_chars]


def _parse_project_yaml(path: Path) -> dict[str, str]:
    text = _read_text(path, 4000)
    result: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line or line.startswith(" "):
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in {"project", "title", "status", "primary_deliverable"}:
            result[key] = value.strip().strip('"\'')
    return result


def load_project_contexts(roots: Iterable[Path] | None = None) -> list[ProjectContext]:
    contexts: list[ProjectContext] = []
    for root in roots or default_project_roots():
        try:
            project_dirs = [p for p in Path(root).expanduser().iterdir() if p.is_dir()]
        except OSError:
            continue
        for project_dir in sorted(project_dirs, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)[:_MAX_PROJECTS]:
            meta = _parse_project_yaml(project_dir / "meta" / "project.yaml")
            slug = meta.get("project") or project_dir.name
            title = meta.get("title") or slug.replace("-", " ")
            status = meta.get("status", "")
            context_path = project_dir / "PROJECT_CONTEXT.md"
            context_text = _read_text(context_path) if context_path.exists() else ""
            meta_text = _read_text(project_dir / "meta" / "project.yaml", 4000)
            # Include a shallow file/path inventory because artifact names often
            # carry the strongest disambiguating signal.
            inventory: list[str] = []
            try:
                for child in project_dir.rglob("*"):
                    if child.is_file():
                        rel = child.relative_to(project_dir).as_posix()
                        if any(part.startswith(".") for part in child.relative_to(project_dir).parts):
                            continue
                        inventory.append(rel)
                        if len(inventory) >= 80:
                            break
            except OSError:
                pass
            text = "\n".join([slug, title, status, context_text, meta_text, "\n".join(inventory)])
            contexts.append(
                ProjectContext(
                    slug=slug,
                    title=title,
                    path=project_dir,
                    context_path=context_path if context_path.exists() else None,
                    text=text[:12000],
                    status=status,
                )
            )
    return contexts


def _session_hint_slugs(agent: Any, seed_text: str) -> set[str]:
    db = getattr(agent, "_session_db", None) if agent is not None else None
    if db is None:
        return set()
    tokens = list(_tokenize(seed_text))
    if not tokens:
        return set()
    # Prefer distinctive business/project terms over continuation boilerplate.
    tokens = sorted(tokens, key=lambda t: (t in {"inteligencia", "artificial", "negocio", "ensinar"}, len(t)), reverse=True)
    query = " OR ".join(tokens[:8])
    slugs: set[str] = set()
    try:
        matches = db.search_messages(query, role_filter=["user", "assistant"], limit=8, sort="newest")
    except Exception:
        return set()
    for match in matches:
        blob = "\n".join(str(match.get(k) or "") for k in ("snippet", "session_id", "source"))
        for ctx in match.get("context") or []:
            if isinstance(ctx, Mapping):
                blob += "\n" + str(ctx.get("content") or "")
        for m in re.finditer(r"research/projects/([a-zA-Z0-9_-]+)", blob):
            slugs.add(m.group(1))
        for m in re.finditer(r"\bproject:\s*([a-zA-Z0-9_-]+)", blob):
            slugs.add(m.group(1))
        for m in re.finditer(r"\b([a-z0-9]+(?:-[a-z0-9]+){2,})\b", normalize_text(blob)):
            candidate = m.group(1)
            if any(part in candidate for part in ("implementation", "education", "imobiliarias", "identity", "security")):
                slugs.add(candidate)
    return slugs


def score_project(
    project: ProjectContext,
    seed_text: str,
    *,
    message_tokens: set[str] | None = None,
    hinted_slugs: set[str] | None = None,
) -> tuple[float, str]:
    seed_tokens = _tokenize(seed_text)
    project_tokens = _tokenize(project.text)
    if not seed_tokens:
        return 0.0, "no distinctive seed tokens"
    overlap = seed_tokens & project_tokens
    score = float(len(overlap))
    reasons = []
    if overlap:
        reasons.append("token overlap: " + ", ".join(sorted(overlap)[:12]))
    norm_seed = normalize_text(seed_text)
    norm_project = normalize_text(project.text)
    for phrase, weight in [
        ("campo imenso", 4.0),
        ("ensinar", 2.0),
        ("implementar ia", 3.0),
        ("negocio", 2.0),
        ("pmes", 2.0),
        ("imobiliarias", 3.0),
        ("ai implementation", 2.5),
        ("implementation education", 3.0),
    ]:
        if phrase in norm_seed and phrase in norm_project:
            score += weight
            reasons.append(f"phrase '{phrase}'")
    # Cross-session hint: preserved as a soft signal (was +8.0, now +2.0).
    # Continuity feature is intact; the signal is no longer decisive on its
    # own. The evidence gate in resolve_active_topic() enforces that the user
    # actually said something about the topic this turn.
    if project.slug in (hinted_slugs or set()):
        score += _SESSION_HINT_WEIGHT
        reasons.append(f"session-search hinted slug (+{_SESSION_HINT_WEIGHT})")
    # Avoid letting global/high-priority projects win when a local business/SMB
    # project has explicit overlap. This is not a hard-coded block; it only
    # penalizes unrelated Agentic/Machine Identity contexts for business-SMB seeds.
    if any(term in norm_seed for term in ("negocio", "pme", "implementar ia", "campo imenso", "imobiliaria")):
        if any(term in normalize_text(project.slug + " " + project.title) for term in ("identity", "agentic", "machine-identity")):
            score -= 3.0
            reasons.append("penalty: global identity topic conflicts with business-SMB seed")
    # Track how much of the overlap comes from the current message vs history,
    # for the evidence gate.
    if message_tokens is not None and overlap:
        message_overlap = message_tokens & project_tokens
        reasons.append(
            f"message-evidence: {len(message_overlap)} token(s) in current turn"
        )
    return max(score, 0.0), "; ".join(reasons) or "weak lexical match"


def _extract_paths(project: ProjectContext) -> tuple[str, ...]:
    paths = [str(project.path)]
    if project.context_path:
        paths.append(str(project.context_path))
    for rel in (
        "meta/project.yaml",
        "final/reports/relatorio-ia-pmes.md",
        "final/exports/relatorio-ia-pmes.pdf",
        "execution/imobiliarias/README.md",
    ):
        p = project.path / rel
        if p.exists():
            paths.append(str(p))
    return tuple(dict.fromkeys(paths))[:8]


def _extract_last_artifacts(project: ProjectContext) -> tuple[str, ...]:
    artifacts: list[str] = []
    for marker in ("last_artifacts:", "Canonical files", "## Canonical files"):
        idx = project.text.find(marker)
        if idx >= 0:
            section = project.text[idx: idx + 1800]
            for line in section.splitlines():
                stripped = line.strip().lstrip("-").strip().strip("`")
                if "/" in stripped or stripped.endswith((".md", ".pdf", ".png", ".zip", ".html")):
                    artifacts.append(stripped)
            break
    if not artifacts:
        for rel in (
            "final/exports/relatorio-ia-pmes.pdf",
            "execution/imobiliarias/final/package/follow-up-inteligente-imobiliarias-mvp.zip",
            "execution/imobiliarias/final/site/index.html",
        ):
            if (project.path / rel).exists():
                artifacts.append(rel)
    return tuple(dict.fromkeys(artifacts))[:8]


def resolve_active_topic(
    user_message: Any,
    conversation_history: Sequence[Mapping[str, Any]] | None = None,
    *,
    agent: Any = None,
    project_roots: Iterable[Path] | None = None,
    min_confidence: float | None = None,
    min_topic_evidence: int | None = None,
    logger: Any = None,
) -> ActiveTopicPacket | None:
    if min_confidence is None:
        min_confidence = _DEFAULT_MIN_CONFIDENCE
    if min_topic_evidence is None:
        min_topic_evidence = _MIN_TOPIC_EVIDENCE_TOKENS
    if not is_continuation_like(user_message):
        return None
    seed_text = build_seed_text(user_message, conversation_history, agent=agent)
    projects = load_project_contexts(project_roots)
    if not projects:
        return None
    message_tokens = _message_topic_tokens(user_message)
    hinted_slugs = _session_hint_slugs(agent, seed_text)
    scored = []
    for project in projects:
        score, why = score_project(
            project,
            seed_text,
            message_tokens=message_tokens,
            hinted_slugs=hinted_slugs,
        )
        scored.append((score, project, why))
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best, why = scored[0]
    if best_score <= 0:
        return None
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    confidence = min(0.95, best_score / (best_score + runner_up + 2.0))
    # Evidence gate: the current user message must contribute at least
    # `min_topic_evidence` topical tokens that overlap with the best project.
    # Cross-session hint is supportive but cannot alone unlock a packet.
    # Structural turns ("1, 2, 3", "ok", "Discord, Telegram, CLI") score 0 here.
    message_overlap_count = len(message_tokens & _tokenize(best.text))
    if message_overlap_count < min_topic_evidence:
        if logger is not None:
            try:
                logger.info(
                    "active_topic_resolver: evidence-gate reject slug=%s "
                    "session=%s platform=%s message_overlap=%d required=%d "
                    "best_score=%.2f confidence=%.2f",
                    best.slug,
                    getattr(agent, "session_id", None) or "none",
                    getattr(agent, "platform", None) or "",
                    message_overlap_count,
                    min_topic_evidence,
                    best_score,
                    confidence,
                )
            except Exception:
                pass
        return None
    if confidence < min_confidence:
        if logger is not None:
            try:
                logger.info(
                    "active_topic_resolver: confidence-gate reject slug=%s "
                    "session=%s platform=%s confidence=%.2f threshold=%.2f",
                    best.slug,
                    getattr(agent, "session_id", None) or "none",
                    getattr(agent, "platform", None) or "",
                    confidence,
                    min_confidence,
                )
            except Exception:
                pass
        return None
    # Accepted — emit structured log so future debugging has a real trail.
    if logger is not None:
        try:
            logger.info(
                "active_topic_resolver: accepted slug=%s session=%s platform=%s "
                "confidence=%.2f best_score=%.2f message_overlap=%d "
                "hinted=%s",
                best.slug,
                getattr(agent, "session_id", None) or "none",
                getattr(agent, "platform", None) or "",
                confidence,
                best_score,
                message_overlap_count,
                ",".join(sorted(hinted_slugs)) if hinted_slugs else "",
            )
        except Exception:
            pass
    current_open_loop = "Continue from the resolved project context; if proposing next focus areas, stay inside this project boundary."
    instructions = [
        "Resolve this continuation to the named project before answering.",
        "If the user asks for suggestions, suggest next actions within this project, not a different global priority.",
        "State the resolved assumption briefly when ambiguity is plausible.",
    ]
    # Preserve explicit anti-confusion lines from PROJECT_CONTEXT.md when present.
    anti_confusion = []
    for line in best.text.splitlines():
        norm = normalize_text(line)
        if "does not mean" in norm or "nao" in norm and ("agentic" in norm or "education" in norm or "setor" in norm):
            anti_confusion.append(line.strip("- "))
    instructions.extend([line for line in anti_confusion if line][:4])
    return ActiveTopicPacket(
        topic_label=best.title,
        project_slug=best.slug,
        canonical_paths=_extract_paths(best),
        last_artifacts=_extract_last_artifacts(best),
        current_open_loop=current_open_loop,
        confidence=confidence,
        why=why[:400],
        instructions=tuple(instructions),
    )


def build_active_topic_context(
    user_message: Any,
    conversation_history: Sequence[Mapping[str, Any]] | None = None,
    *,
    agent: Any = None,
    project_roots: Iterable[Path] | None = None,
) -> str:
    packet = resolve_active_topic(
        user_message,
        conversation_history,
        agent=agent,
        project_roots=project_roots,
    )
    if packet is None:
        return ""
    text = packet.format_for_user_message()
    if len(text) > _MAX_CONTEXT_CHARS:
        return text[: _MAX_CONTEXT_CHARS - 32] + "\n</active_topic_context>"
    return text
