"""Ollama residency inspection command.

Read-only by default: reports currently resident Ollama models, expected
coexistence for Krishna's local memory stack, and whether the embedding model is
transient rather than occupying VRAM.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable, Dict, Optional

Runner = Callable[..., Any]

EXPECTED_CRON_MODEL = "phi4-mini:latest"
EXPECTED_GRAPHITI_MODEL = "qwen3.5:latest"
TRANSIENT_EMBEDDING_MODEL = "nomic-embed-text:latest"


def parse_ollama_ps(output: str) -> list[Dict[str, str]]:
    """Parse `ollama ps` output into row dictionaries."""
    rows: list[Dict[str, str]] = []
    lines = [line.rstrip() for line in (output or "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return rows
    for line in lines[1:]:
        import re

        parts = re.split(r"\s{2,}", line.strip())
        if len(parts) >= 5:
            name, model_id, size, processor, until = parts[0], parts[1], parts[2], parts[3], parts[4]
        else:
            tokens = line.split()
            if len(tokens) < 2:
                continue
            name = tokens[0]
            model_id = tokens[1]
            # Best-effort fallback for single-spaced synthetic/test output.
            size = " ".join(tokens[2:4]) if len(tokens) >= 4 else ""
            processor = " ".join(tokens[4:6]) if len(tokens) >= 6 else (tokens[4] if len(tokens) > 4 else "")
            until = " ".join(tokens[6:]) if len(tokens) > 6 else ""
        rows.append({
            "name": name,
            "id": model_id,
            "size": size,
            "processor": processor,
            "until": until,
            "raw": line,
        })
    return rows


def parse_nvidia_smi_csv(output: str) -> Dict[str, Any]:
    """Parse `nvidia-smi --query-compute-apps=used_memory,total_memory --format=csv,noheader,nounits`."""
    entries = []
    total_used = 0
    total_capacity = 0
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            used = int(float(parts[0]))
            total = int(float(parts[1]))
        except ValueError:
            continue
        entries.append({"used_mib": used, "total_mib": total})
        total_used += used
        total_capacity = max(total_capacity, total)
    return {
        "entries": entries,
        "total_used_mib": total_used,
        "max_total_mib": total_capacity or None,
    }


def build_ollama_residency_report(
    *,
    ollama_ps_output: str,
    nvidia_smi_output: str = "",
) -> Dict[str, Any]:
    models = parse_ollama_ps(ollama_ps_output)
    loaded_names = [row["name"] for row in models]
    loaded_set = set(loaded_names)
    vram = parse_nvidia_smi_csv(nvidia_smi_output)
    phi4_loaded = EXPECTED_CRON_MODEL in loaded_set
    qwen_loaded = EXPECTED_GRAPHITI_MODEL in loaded_set
    nomic_loaded = TRANSIENT_EMBEDDING_MODEL in loaded_set

    recommendations: list[Dict[str, str]] = []
    if not phi4_loaded:
        recommendations.append({"code": "ollama_phi4_cron_model_not_resident", "severity": "info"})
    if not qwen_loaded:
        recommendations.append({"code": "ollama_qwen35_graphiti_model_not_resident", "severity": "info"})
    if nomic_loaded:
        recommendations.append({
            "code": "ollama_embedding_model_resident",
            "severity": "warn",
            "message": "nomic-embed-text should be transient after Honcho embedding requests; unload if it evicts phi4-mini/qwen3.5.",
        })

    return {
        "success": True,
        "models": {
            "loaded_names": loaded_names,
            "loaded": models,
        },
        "expectations": {
            "cron_model": EXPECTED_CRON_MODEL,
            "graphiti_model": EXPECTED_GRAPHITI_MODEL,
            "transient_embedding_model": TRANSIENT_EMBEDDING_MODEL,
            "phi4_mini_loaded": phi4_loaded,
            "qwen35_loaded": qwen_loaded,
            "expected_coexistence_ok": phi4_loaded and qwen_loaded and not nomic_loaded,
            "nomic_embedding_transient_ok": not nomic_loaded,
        },
        "vram": vram,
        "recommendations": recommendations,
    }


def _run_command(runner: Runner, command: list[str], *, timeout: int = 10) -> Any:
    return runner(command, check=False, text=True, capture_output=True, timeout=timeout)


def collect_ollama_residency_report(*, runner: Optional[Runner] = None, fix: bool = False) -> Dict[str, Any]:
    runner = runner or subprocess.run
    fix_result = {"attempted": False, "unloaded": [], "errors": []}
    try:
        ps_proc = _run_command(runner, ["ollama", "ps"], timeout=10)
        ollama_output = ps_proc.stdout if getattr(ps_proc, "returncode", 1) == 0 else ""
        ollama_error = getattr(ps_proc, "stderr", "") if getattr(ps_proc, "returncode", 1) != 0 else ""
    except Exception as exc:
        ollama_output = ""
        ollama_error = str(exc)

    initial_report = build_ollama_residency_report(
        ollama_ps_output=ollama_output,
        nvidia_smi_output="",
    )
    if fix and TRANSIENT_EMBEDDING_MODEL in initial_report["models"]["loaded_names"]:
        fix_result["attempted"] = True
        try:
            unload_proc = _run_command(
                runner,
                ["ollama", "stop", TRANSIENT_EMBEDDING_MODEL],
                timeout=20,
            )
            if getattr(unload_proc, "returncode", 1) == 0:
                fix_result["unloaded"].append(TRANSIENT_EMBEDDING_MODEL)
            else:
                fix_result["errors"].append(getattr(unload_proc, "stderr", "") or "ollama unload failed")
        except Exception as exc:
            fix_result["errors"].append(str(exc))
        try:
            ps_proc = _run_command(runner, ["ollama", "ps"], timeout=10)
            ollama_output = ps_proc.stdout if getattr(ps_proc, "returncode", 1) == 0 else ollama_output
        except Exception:
            pass

    try:
        gpu_proc = _run_command(
            runner,
            [
                "nvidia-smi",
                "--query-compute-apps=used_memory,total_memory",
                "--format=csv,noheader,nounits",
            ],
            timeout=10,
        )
        gpu_output = gpu_proc.stdout if getattr(gpu_proc, "returncode", 1) == 0 else ""
    except Exception:
        gpu_output = ""

    report = build_ollama_residency_report(
        ollama_ps_output=ollama_output,
        nvidia_smi_output=gpu_output,
    )
    if fix:
        report["fix"] = fix_result
    if ollama_error:
        report["success"] = False
        report["error"] = ollama_error
        report["recommendations"].append({"code": "ollama_ps_unavailable", "severity": "warn"})
    return report


def ollama_residency_command(args, *, runner: Optional[Runner] = None) -> None:
    payload = collect_ollama_residency_report(runner=runner, fix=bool(getattr(args, "fix", False)))
    if bool(getattr(args, "json", False)):
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print(json.dumps(payload, indent=2, ensure_ascii=False))
