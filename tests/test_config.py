from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from dropline.config import load_settings, validate_live_settings

from tests.helpers import create_test_repo, load_test_settings


class ConfigTests(unittest.TestCase):
    def test_load_settings_reads_config_values_and_builds_document_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = load_test_settings(root)
            self.assertEqual(settings.llm.stage1.provider, "openai")
            self.assertEqual(settings.llm.stage2.provider, "openai")
            self.assertEqual(settings.llm.image.provider, "openai")
            self.assertEqual(settings.mail.character_mailbox.smtp.security, "ssl")
            self.assertTrue(settings.llm.image.enabled)
            self.assertEqual(settings.llm.language, "简体中文")
            self.assertEqual(settings.providers["openai"].api_key, "test-openai-key")
            self.assertEqual(settings.providers["openai"].text_model, "gpt-5.4")
            self.assertEqual(settings.providers["openai"].image_model, "gpt-image-1.5")
            self.assertEqual(settings.providers["gemini"].api_key, "test-gemini-key")
            self.assertEqual(settings.providers["gemini"].text_model, "gemini-3.1-pro-preview")
            self.assertEqual(settings.providers["gemini"].image_model, "gemini-3-pro-image-preview")
            self.assertEqual(settings.providers["anthropic"].api_key, "test-anthropic-key")
            self.assertEqual(settings.providers["anthropic"].text_model, "claude-opus-4-7")
            self.assertEqual(settings.providers["grok"].api_key, "test-xai-key")
            self.assertEqual(settings.providers["grok"].text_model, "grok-4.20-0309-reasoning")
            self.assertEqual(settings.providers["grok"].image_model, "grok-imagine-image-pro")
            self.assertEqual(settings.providers["qwen"].api_key, "test-dashscope-key")
            self.assertEqual(settings.providers["qwen"].text_model, "qwen3-max-2026-01-23")
            self.assertEqual(settings.providers["qwen"].image_model, "qwen-image-2.0-pro")
            self.assertEqual(settings.providers["deepseek"].api_key, "test-deepseek-key")
            self.assertEqual(settings.providers["deepseek"].text_model, "deepseek-v4-pro")
            self.assertEqual(settings.providers["glm"].api_key, "test-glm-key")
            self.assertEqual(settings.providers["glm"].text_model, "GLM-5.1")
            self.assertEqual(settings.providers["doubao"].api_key, "test-doubao-key")
            self.assertEqual(settings.providers["doubao"].text_model, "doubao-seed-2-0-pro-260215")
            self.assertEqual(settings.providers["doubao"].image_model, "doubao-seedream-5-0-260128")
            self.assertTrue(settings.storage.sqlite_path.is_absolute())
            self.assertEqual(
                settings.documents.persona,
                root / "profiles" / "characters" / "keqing" / "prompt" / "persona.md",
            )
            self.assertEqual(
                settings.documents.user_episode_dir,
                root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends" / "episodes" / "user",
            )
            self.assertEqual(
                settings.documents.session_archive_dir,
                root / "profiles" / "characters" / "keqing" / "sessions" / "childhood-friends" / "archive",
            )

    def test_load_settings_uses_explicit_repo_root_for_external_config(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as config_dir:
            root = Path(repo_dir).resolve()
            create_test_repo(root)
            external_config = Path(config_dir).resolve() / "config.local.yaml"
            payload = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            payload["project"]["repo_root"] = str(root)
            external_config.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(external_config)

            self.assertEqual(settings.repo_root, root)
            self.assertEqual(settings.storage.sqlite_path, root / "state.db")
            self.assertEqual(
                settings.documents.persona,
                root / "profiles" / "characters" / "keqing" / "prompt" / "persona.md",
            )

    def test_load_settings_defaults_repo_root_to_config_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)

            settings = load_settings(root / "config.yaml")

            self.assertEqual(settings.repo_root, root)

    def test_load_settings_rejects_relative_repo_root(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir, tempfile.TemporaryDirectory() as config_dir:
            root = Path(repo_dir).resolve()
            create_test_repo(root)
            external_config = Path(config_dir).resolve() / "config.local.yaml"
            payload = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
            payload["project"]["repo_root"] = "."
            external_config.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "project.repo_root"):
                load_settings(external_config)

    def test_load_settings_keeps_placeholder_shaped_strings_literal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('host: "imap.example.com"', 'host: "${KEQING_IMAP_HOST}"')
            config_text = config_text.replace('username: "character@example.com"', 'username: "${KEQING_IMAP_USERNAME}"', 1)
            config_text = config_text.replace('password: "secret"', 'password: "${KEQING_IMAP_PASSWORD}"', 1)
            config_text = config_text.replace('host: "smtp.example.com"', 'host: "${KEQING_SMTP_HOST}"')
            config_text = config_text.replace('username: "character@example.com"', 'username: "${KEQING_SMTP_USERNAME}"', 1)
            config_text = config_text.replace('password: "secret"', 'password: "${KEQING_SMTP_PASSWORD}"', 1)
            config_path.write_text(config_text, encoding="utf-8")
            settings = load_settings(config_path)
            self.assertEqual(settings.mail.character_mailbox.imap.host, "${KEQING_IMAP_HOST}")
            self.assertEqual(settings.mail.character_mailbox.smtp.host, "${KEQING_SMTP_HOST}")

    def test_validate_live_settings_reports_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["openai"]["api_key"] = ""
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "openai.api_key"):
                load_settings(config_path)

    def test_load_settings_requires_smtp_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["mail"]["character_mailbox"]["smtp"]["security"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "mail.character_mailbox.smtp.security"):
                load_settings(config_path)

    def test_load_settings_rejects_invalid_smtp_security(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["mail"]["character_mailbox"]["smtp"]["security"] = "tls"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "mail.character_mailbox.smtp.security"):
                load_settings(config_path)

    def test_load_settings_does_not_accept_legacy_smtp_ssl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["mail"]["character_mailbox"]["smtp"]["ssl"] = True
            del payload["mail"]["character_mailbox"]["smtp"]["security"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "mail.character_mailbox.smtp.security"):
                load_settings(config_path)

    def test_validate_live_settings_supports_mixed_stage_providers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["stage1"]["provider"] = "anthropic"
            payload["llm"]["stage2"]["provider"] = "anthropic"
            payload["llm"]["image"]["provider"] = "grok"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings = load_settings(config_path)
            self.assertEqual(settings.llm.stage1.provider, "anthropic")
            self.assertEqual(settings.llm.stage2.provider, "anthropic")
            self.assertEqual(settings.llm.image.provider, "grok")
            validate_live_settings(settings)

    def test_validate_live_settings_rejects_placeholder_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["mail"]["primary_user_recipient"] = "REPLACE_ME_PRIMARY_USER_EMAIL"
            payload["openai"]["api_key"] = "REPLACE_ME_OPENAI_API_KEY"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings = load_settings(config_path)
            with self.assertRaisesRegex(ValueError, "mail.primary_user_recipient, openai.api_key|openai.api_key, mail.primary_user_recipient"):
                validate_live_settings(settings)

    def test_validate_live_settings_accepts_qwen_as_image_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["image"]["provider"] = "qwen"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings = load_settings(config_path)
            self.assertEqual(settings.llm.image.provider, "qwen")
            validate_live_settings(settings)

    def test_validate_live_settings_accepts_doubao_as_image_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["image"]["provider"] = "doubao"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings = load_settings(config_path)
            self.assertEqual(settings.llm.image.provider, "doubao")
            validate_live_settings(settings)

    def test_validate_live_settings_rejects_unsupported_image_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["image"]["provider"] = "anthropic"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "llm.image.provider"):
                load_settings(config_path)

    def test_validate_live_settings_rejects_new_text_only_provider_for_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["image"]["provider"] = "deepseek"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "llm.image.provider"):
                load_settings(config_path)

    def test_validate_live_settings_skips_image_requirements_when_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["stage1"]["provider"] = "gemini"
            payload["llm"]["stage2"]["provider"] = "gemini"
            payload["llm"]["image"]["enabled"] = False
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            settings = load_settings(config_path)
            self.assertFalse(settings.llm.image.enabled)
            validate_live_settings(settings)

    def test_load_settings_accepts_only_current_provider_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            for provider in ("gemini", "anthropic", "grok", "qwen", "deepseek", "glm", "doubao"):
                del payload[provider]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(config_path)

            self.assertEqual(set(settings.providers), {"openai"})
            self.assertEqual(settings.providers["openai"].api_key, "test-openai-key")
            self.assertEqual(settings.providers["openai"].text_model, "gpt-5.4")
            self.assertEqual(settings.providers["openai"].image_model, "gpt-image-1.5")

    def test_load_settings_ignores_malformed_unused_provider_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["gemini"]["api_key"]
            del payload["gemini"]["text_model"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(config_path)

            self.assertEqual(settings.llm.stage1.provider, "openai")
            self.assertNotIn("gemini", settings.providers)

    def test_load_settings_requires_current_provider_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["openai"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "openai"):
                load_settings(config_path)

    def test_load_settings_requires_explicit_provider_text_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["openai"]["text_model"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "openai.text_model"):
                load_settings(config_path)

    def test_load_settings_requires_current_image_provider_image_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["openai"]["image_model"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "openai.image_model"):
                load_settings(config_path)

    def test_load_settings_does_not_require_unused_provider_text_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["gemini"]["text_model"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(config_path)

            self.assertNotIn("gemini", settings.providers)

    def test_load_settings_requires_llm_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            del payload["llm"]["language"]
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "llm.language"):
                load_settings(config_path)

    def test_load_settings_rejects_unsupported_llm_language(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["llm"]["language"] = "english"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "llm.language"):
                load_settings(config_path)

    def test_load_settings_rejects_invalid_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["project"]["timezone"] = "Not/AZone"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "project.timezone"):
                load_settings(config_path)

    def test_load_settings_rejects_malformed_schedule_times(self) -> None:
        cases = [
            ("window_start", "25:99"),
            ("window_end", "7:00"),
            ("daily_target_pick_time", "tonight"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir).resolve()
                    create_test_repo(root)
                    config_path = root / "config.yaml"
                    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                    payload["schedule"][key] = value
                    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, f"schedule.{key}"):
                        load_settings(config_path)

    def test_load_settings_accepts_cross_midnight_schedule_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["schedule"]["window_start"] = "23:00"
            payload["schedule"]["window_end"] = "02:00"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(config_path)

        self.assertEqual(settings.schedule.window_start, "23:00")
        self.assertEqual(settings.schedule.window_end, "02:00")

    def test_load_settings_rejects_retry_after_minutes_of_a_day_or_more(self) -> None:
        # A back-off of 24 hours or more would also block the catch-up of yesterday's missed send.
        for value, accepted in ((1439, True), (1440, False), (2880, False)):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir).resolve()
                create_test_repo(root)
                config_path = root / "config.yaml"
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                payload["schedule"]["retry_after_minutes"] = value
                config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
                if accepted:
                    self.assertEqual(load_settings(config_path).schedule.retry_after_minutes, value)
                else:
                    with self.assertRaisesRegex(ValueError, r"schedule\.retry_after_minutes.*< 1440"):
                        load_settings(config_path)

    def test_load_settings_rejects_non_positive_check_interval(self) -> None:
        for value in (0, -1):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    root = Path(tmp_dir).resolve()
                    create_test_repo(root)
                    config_path = root / "config.yaml"
                    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                    payload["schedule"]["check_every_minutes"] = value
                    config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "schedule.check_every_minutes"):
                        load_settings(config_path)

    def test_load_settings_applies_failure_handling_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)

            settings = load_test_settings(root)

        self.assertEqual(settings.schedule.retry_after_minutes, 120)
        self.assertEqual(settings.schedule.max_runs_per_day, 3)
        self.assertEqual(settings.schedule.max_batch_attempts, 3)
        self.assertTrue(settings.mail.alerts.enabled)
        self.assertEqual(settings.mail.alerts.recipient, "user@example.com")
        self.assertEqual(settings.mail.alerts.after_failures, 2)
        self.assertEqual(settings.llm.image.on_failure, "abort")

    def test_load_settings_reads_failure_handling_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["schedule"].update({"retry_after_minutes": 45, "max_runs_per_day": 5, "max_batch_attempts": 1})
            payload["mail"]["alerts"] = {"enabled": False, "recipient": "ops@example.com", "after_failures": 4}
            payload["llm"]["image"]["on_failure"] = "TEXT_ONLY"
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")

            settings = load_settings(config_path)

        self.assertEqual(settings.schedule.retry_after_minutes, 45)
        self.assertEqual(settings.schedule.max_runs_per_day, 5)
        self.assertEqual(settings.schedule.max_batch_attempts, 1)
        self.assertFalse(settings.mail.alerts.enabled)
        self.assertEqual(settings.mail.alerts.recipient, "ops@example.com")
        self.assertEqual(settings.mail.alerts.after_failures, 4)
        self.assertEqual(settings.llm.image.on_failure, "text_only")

    def test_load_settings_rejects_invalid_failure_handling_values(self) -> None:
        cases = (
            (("schedule", "retry_after_minutes"), 0, "schedule.retry_after_minutes"),
            (("schedule", "max_runs_per_day"), "3", "schedule.max_runs_per_day"),
            (("schedule", "max_batch_attempts"), -1, "schedule.max_batch_attempts"),
            (("mail", "alerts"), "yes", "mail.alerts"),
            (("mail", "alerts", "enabled"), "true", "mail.alerts.enabled"),
            (("mail", "alerts", "after_failures"), 0, "mail.alerts.after_failures"),
            (("mail", "alerts", "recipient"), "", "mail.alerts.recipient"),
            (("llm", "image", "on_failure"), "retry", "llm.image.on_failure"),
        )
        for keys, value, expected_path in cases:
            with self.subTest(path=expected_path), tempfile.TemporaryDirectory() as tmp_dir:
                root = Path(tmp_dir).resolve()
                create_test_repo(root)
                config_path = root / "config.yaml"
                payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                target = payload
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
                config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, expected_path):
                    load_settings(config_path)

    def test_validate_live_settings_requires_alert_recipient_only_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            config_path = root / "config.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["mail"]["alerts"] = {"recipient": "REPLACE_ME_ALERT_RECIPIENT"}
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mail.alerts.recipient"):
                validate_live_settings(load_settings(config_path))

            payload["mail"]["alerts"]["enabled"] = False
            config_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
            validate_live_settings(load_settings(config_path))


if __name__ == "__main__":
    unittest.main()
