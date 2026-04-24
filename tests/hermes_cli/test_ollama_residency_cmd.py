import json
from types import SimpleNamespace

from hermes_cli.ollama_residency_cmd import build_ollama_residency_report, ollama_residency_command, parse_ollama_ps


OLLAMA_PS = """NAME              ID              SIZE      PROCESSOR    UNTIL
phi4-mini:latest  abc123          3.3 GB    100% GPU     4 minutes from now
qwen3.5:latest    def456          8.8 GB    100% GPU     4 minutes from now
"""


def test_parse_ollama_ps_extracts_loaded_models_and_raw_rows():
    rows = parse_ollama_ps(OLLAMA_PS)

    assert [row["name"] for row in rows] == ["phi4-mini:latest", "qwen3.5:latest"]
    assert rows[0]["size"] == "3.3 GB"
    assert rows[0]["processor"] == "100% GPU"
    assert "4 minutes" in rows[0]["until"]


def test_build_ollama_residency_report_flags_expected_coexistence_and_transient_embedding():
    report = build_ollama_residency_report(
        ollama_ps_output=OLLAMA_PS,
        nvidia_smi_output="1000, 12000\n8800, 12000\n",
    )

    assert report["success"] is True
    assert report["models"]["loaded_names"] == ["phi4-mini:latest", "qwen3.5:latest"]
    assert report["expectations"]["phi4_mini_loaded"] is True
    assert report["expectations"]["qwen35_loaded"] is True
    assert report["expectations"]["expected_coexistence_ok"] is True
    assert report["expectations"]["nomic_embedding_transient_ok"] is True
    assert report["vram"]["total_used_mib"] == 9800
    assert not any(item["severity"] == "warn" for item in report["recommendations"])


def test_build_ollama_residency_report_warns_when_embedding_is_resident_and_qwen_missing():
    report = build_ollama_residency_report(
        ollama_ps_output="""NAME ID SIZE PROCESSOR UNTIL
phi4-mini:latest a 3.3 GB 100% GPU 4 minutes
nomic-embed-text:latest b 1.0 GB 100% GPU 4 minutes
""",
        nvidia_smi_output="",
    )

    codes = {item["code"] for item in report["recommendations"]}
    assert report["expectations"]["expected_coexistence_ok"] is False
    assert report["expectations"]["nomic_embedding_transient_ok"] is False
    assert "ollama_qwen35_graphiti_model_not_resident" in codes
    assert "ollama_embedding_model_resident" in codes


def test_ollama_residency_command_outputs_json(capsys):
    def fake_runner(command, **kwargs):
        if command[:2] == ["ollama", "ps"]:
            return SimpleNamespace(returncode=0, stdout=OLLAMA_PS, stderr="")
        if command and command[0] == "nvidia-smi":
            return SimpleNamespace(returncode=0, stdout="3300, 12000\n8800, 12000\n", stderr="")
        raise AssertionError(command)

    ollama_residency_command(SimpleNamespace(json=True, fix=False), runner=fake_runner)

    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    assert payload["expectations"]["expected_coexistence_ok"] is True
    assert payload["vram"]["total_used_mib"] == 12100


def test_ollama_residency_fix_unloads_transient_embedding_model(capsys):
    calls = []
    resident_embedding = """NAME ID SIZE PROCESSOR UNTIL
nomic-embed-text:latest b 1.0 GB 100% GPU 4 minutes
"""

    def fake_runner(command, **kwargs):
        calls.append(command)
        if command[:2] == ["ollama", "ps"]:
            return SimpleNamespace(returncode=0, stdout=resident_embedding, stderr="")
        if command[:2] == ["ollama", "stop"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command and command[0] == "nvidia-smi":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(command)

    ollama_residency_command(SimpleNamespace(json=True, fix=True), runner=fake_runner)

    payload = json.loads(capsys.readouterr().out)
    assert payload["fix"]["attempted"] is True
    assert payload["fix"]["unloaded"] == ["nomic-embed-text:latest"]
    assert ["ollama", "stop", "nomic-embed-text:latest"] in calls
