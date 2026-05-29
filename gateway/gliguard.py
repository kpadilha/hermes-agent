"""Local GLiGuard gateway ingress helpers.

The gateway uses this module to call a localhost guardrail sidecar in
shadow/enforce mode and to persist sanitized JSONL shadow telemetry.  The log
must never contain raw user text, system prompts, memory, or tool outputs.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

from hermes_constants import get_hermes_home


@dataclass
class GliguardConfig:
    enabled: bool = False
    mode: str = "shadow"
    url: str = "http://127.0.0.1:8766/moderate"
    timeout_ms: int = 500
    fail_open: bool = True
    shadow_log_path: Path = field(default_factory=lambda: get_hermes_home() / "logs" / "gliguard_shadow.jsonl")


@dataclass
class GliguardDecision:
    ok: bool
    decision: str
    reasons: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision != "blocked"


@dataclass
class GliguardModerationResult:
    allowed: bool
    decision: str
    reasons: list[str] = field(default_factory=list)
    latency_ms: float | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _get_nested(config: Any, *keys: str) -> Any:
    cur = config
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            cur = getattr(cur, key, None)
        if cur is None:
            return None
    return cur


def config_from_mapping(config: Any) -> GliguardConfig:
    data = _get_nested(config, "security", "gliguard")
    if not isinstance(data, dict):
        data = {}
    path = data.get("shadow_log_path") or data.get("log_path")
    return GliguardConfig(
        enabled=bool(data.get("enabled", False)),
        mode=str(data.get("mode", "shadow") or "shadow").lower(),
        url=str(data.get("url", "http://127.0.0.1:8766/moderate") or "http://127.0.0.1:8766/moderate"),
        timeout_ms=int(data.get("timeout_ms", 500) or 500),
        fail_open=bool(data.get("fail_open", True)),
        shadow_log_path=Path(path).expanduser() if path else get_hermes_home() / "logs" / "gliguard_shadow.jsonl",
    )


def _summarize_label_value(value: Any) -> Any:
    """Return a non-textual, label-oriented summary of model output.

    The sidecar's ``raw`` payload is not a stable privacy contract. Treat it as
    potentially sensitive and only retain labels/confidences, small numeric
    telemetry, booleans, and structural placeholders. Arbitrary strings are
    length-only so raw user text cannot leak into JSONL shadow telemetry.
    """

    def one(item: Any) -> Any:
        if isinstance(item, dict):
            label = item.get("label")
            if isinstance(label, str):
                conf = item.get("confidence")
                if isinstance(conf, (int, float)) or isinstance(conf, str):
                    try:
                        return f"{label} {float(conf):.2f}"
                    except Exception:
                        pass
                return {"label_len": len(label)}
            nested = {str(k): one(v) for k, v in item.items()}
            return {k: v for k, v in nested.items() if v is not None}
        if isinstance(item, list):
            out = [one(x) for x in item[:5]]
            return [x for x in out if x is not None]
        if isinstance(item, bool) or isinstance(item, (int, float)):
            return item
        if isinstance(item, str):
            return {"string_len": len(item)}
        return None

    return one(value)


def _raw_summary(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    summary = {str(k): _summarize_label_value(v) for k, v in raw.items()}
    return {k: v for k, v in summary.items() if v is not None}


def build_shadow_event(
    *,
    text: str,
    decision: GliguardDecision | GliguardModerationResult,
    mode: str,
    platform: str,
    chat_type: str | None,
    chat_id: str | None,
    user_id: str | None,
    message_id: str | None,
    session_key: str | None,
) -> dict[str, Any]:
    text = text or ""
    return {
        "event": "gliguard_shadow_decision",
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "decision": decision.decision,
        "would_block": decision.decision in {"unsafe", "blocked"},
        "reasons": list(decision.reasons or []),
        "latency_ms": decision.latency_ms,
        "error": decision.error,
        "platform": platform,
        "chat_type": chat_type,
        "chat_id_hash": hashlib.sha256(str(chat_id or "").encode()).hexdigest() if chat_id else None,
        "user_id_hash": hashlib.sha256(str(user_id or "").encode()).hexdigest() if user_id else None,
        "message_id": str(message_id) if message_id is not None else None,
        "session_key_hash": hashlib.sha256(str(session_key or "").encode()).hexdigest() if session_key else None,
        "text_len": len(text),
        "text_sha256": hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest(),
        "raw_summary": _raw_summary(decision.raw),
    }


def append_shadow_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


async def moderate_text(text: str, cfg: GliguardConfig) -> GliguardModerationResult:
    if not cfg.enabled:
        return GliguardModerationResult(allowed=True, decision="disabled")
    body = {"kind": "prompt", "text": text or ""}
    timeout = aiohttp.ClientTimeout(total=max(cfg.timeout_ms, 1) / 1000)
    started = time.perf_counter()
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(cfg.url, json=body) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, Exception) as exc:
        return GliguardModerationResult(
            allowed=bool(cfg.fail_open),
            decision="error" if cfg.fail_open else "blocked",
            reasons=["gliguard_error"],
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=type(exc).__name__,
        )
    decision = str(data.get("decision") or "safe")
    reasons = data.get("reasons") if isinstance(data.get("reasons"), list) else []
    allowed = not (cfg.mode == "enforce" and decision == "unsafe")
    return GliguardModerationResult(
        allowed=allowed,
        decision="blocked" if not allowed else decision,
        reasons=[str(x) for x in reasons],
        latency_ms=float(data.get("latency_ms") or round((time.perf_counter() - started) * 1000, 2)),
        raw=data.get("raw") if isinstance(data.get("raw"), dict) else {},
    )
