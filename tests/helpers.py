from __future__ import annotations

import json
from pathlib import Path

from dropline.config import load_settings

MINIMAL_CONFIG = """\
project:
  name: "mail-profile-test"
  timezone: "America/Los_Angeles"

schedule:
  enabled: true
  window_start: "19:00"
  window_end: "23:59"
  daily_target_pick_time: "00:05"
  check_every_minutes: 10

mail:
  allowlist_senders:
    - "user@example.com"
  primary_user_recipient: "user@example.com"
  character_mailbox:
    address: "character@example.com"
    imap:
      host: "imap.example.com"
      port: 993
      ssl: true
      username: "character@example.com"
      password: "secret"
      folder: "INBOX"
    smtp:
      host: "smtp.example.com"
      port: 465
      security: "ssl"
      username: "character@example.com"
      password: "secret"

storage:
  sqlite_path: "./state.db"

llm:
  language: "简体中文"
  stage1:
    provider: "openai"
  stage2:
    provider: "openai"
  image:
    enabled: true
    provider: "openai"

openai:
  api_key: "test-openai-key"
  text_model: "gpt-5.4"
  image_model: "gpt-image-1.5"

gemini:
  api_key: "test-gemini-key"
  text_model: "gemini-3.1-pro-preview"
  image_model: "gemini-3-pro-image-preview"

anthropic:
  api_key: "test-anthropic-key"
  text_model: "claude-opus-4-7"

grok:
  api_key: "test-xai-key"
  text_model: "grok-4.20-0309-reasoning"
  image_model: "grok-imagine-image-pro"

qwen:
  api_key: "test-dashscope-key"
  text_model: "qwen3-max-2026-01-23"
  image_model: "qwen-image-2.0-pro"

deepseek:
  api_key: "test-deepseek-key"
  text_model: "deepseek-v4-pro"

glm:
  api_key: "test-glm-key"
  text_model: "GLM-5.1"

doubao:
  api_key: "test-doubao-key"
  text_model: "doubao-seed-2-0-pro-260215"
  image_model: "doubao-seedream-5-0-260128"

logging:
  level: "INFO"
"""


DOCS_FILES = {
    "run_pipeline.md": "# run_pipeline\n\n测试用 pipeline 说明。",
    "run_pipeline.en.md": "# run_pipeline\n\nTest pipeline notes.",
    "memory_promotion_rules.md": "# memory_promotion_rules\n\n少记，不乱记。",
    "memory_promotion_rules.en.md": "# memory_promotion_rules\n\nRemember less, not more.",
}


CHARACTER_PROMPT_FILES = {
    "persona.md": "# persona\n\n刻晴的角色边界。",
    "world_state.md": "# world_state\n\n璃月仍在照常运转。",
    "cast_relations.md": "# cast_relations\n\n主要关系轴。",
    "cast_pool.md": "# cast_pool\n\n可出场角色池。",
    "translation_layer.md": "# translation_layer\n\n现代日常表达自然转译。",
}


RELATIONSHIP_PROMPT_FILES = {
    "relationship.md": "# relationship\n\n青梅竹马，久别重逢。",
}


SESSION_MEMORY_FILES = {
    "user_profile.json": "[]\n",
    "relationship_memory.json": "[]\n",
    "active_arc.json": "[]\n",
    "conflict_notes.json": "[]\n",
}


def load_test_settings(root: Path):
    return load_settings(root / "config.yaml")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def create_test_repo(root: Path) -> None:
    docs_root = root / "docs"
    character_root = root / "profiles" / "characters" / "keqing"
    relationship_root = character_root / "relationships" / "childhood-friends"
    session_root = character_root / "sessions" / "childhood-friends"

    (docs_root).mkdir(parents=True, exist_ok=True)
    (character_root / "prompt").mkdir(parents=True, exist_ok=True)
    (relationship_root / "prompt").mkdir(parents=True, exist_ok=True)
    (session_root / "memory").mkdir(parents=True, exist_ok=True)
    (session_root / "episodes" / "user").mkdir(parents=True, exist_ok=True)
    (session_root / "episodes" / "character").mkdir(parents=True, exist_ok=True)
    (session_root / "archive").mkdir(parents=True, exist_ok=True)

    for name, content in DOCS_FILES.items():
        (docs_root / name).write_text(content, encoding="utf-8")
    for name, content in CHARACTER_PROMPT_FILES.items():
        (character_root / "prompt" / name).write_text(content, encoding="utf-8")
    for name, content in RELATIONSHIP_PROMPT_FILES.items():
        (relationship_root / "prompt" / name).write_text(content, encoding="utf-8")
    for name, content in SESSION_MEMORY_FILES.items():
        (session_root / "memory" / name).write_text(content, encoding="utf-8")

    _write_json(
        character_root / "meta.json",
        {
            "slug": "keqing",
            "character_name": "刻晴",
            "work_title": "原神",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    _write_json(
        relationship_root / "meta.json",
        {
            "slug": "childhood-friends",
            "character_slug": "keqing",
            "name": "青梅竹马",
            "description": "青梅竹马，久别重逢。",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    _write_json(
        session_root / "meta.json",
        {
            "slug": "childhood-friends",
            "character_slug": "keqing",
            "relationship_slug": "childhood-friends",
            "character_name": "刻晴",
            "relationship_name": "青梅竹马",
            "created_at": "2026-01-01T00:00:00",
        },
    )
    _write_json(
        root / "profiles" / "active.json",
        {
            "character_slug": "keqing",
            "relationship_slug": "childhood-friends",
        },
    )

    (root / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
