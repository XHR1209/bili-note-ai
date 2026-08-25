"""config 加载测试：yaml 映射 + 环境变量覆盖。"""

import os

from src.config import load_config


def test_default_config(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.llm.model == "deepseek-chat"
    assert cfg.transcription.backend == "faster-whisper"
    assert cfg.frames.scene_detection.threshold == 27.0
    assert cfg.llm.max_calls_per_video == 200


def test_yaml_override(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        """
llm:
  model: "test-model"
  budget:
    max_calls_per_video: 5
frames:
  scene_detection:
    threshold: 40.0
    min_scene_len: 3.0
transcription:
  backend: "openai-whisper"
bilibili:
  cookie: "SESSDATA=abc"
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("MODEL", raising=False)
    cfg = load_config(yaml_path)
    assert cfg.llm.model == "test-model"
    assert cfg.llm.max_calls_per_video == 5
    assert cfg.frames.scene_detection.threshold == 40.0
    assert cfg.frames.scene_detection.min_scene_len == 3.0
    assert cfg.transcription.backend == "openai-whisper"
    assert cfg.bilibili.cookie == "SESSDATA=abc"


def test_env_override(tmp_path, monkeypatch):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("llm:\n  model: 'from-yaml'\n", encoding="utf-8")
    monkeypatch.setenv("API_KEY", "k")
    monkeypatch.setenv("MODEL", "from-env")
    monkeypatch.setenv("BASE_URL", "https://example.com/v1")
    cfg = load_config(yaml_path)
    assert cfg.llm.model == "from-env"
    assert cfg.llm.api_key == "k"
    assert cfg.llm.base_url == "https://example.com/v1"
