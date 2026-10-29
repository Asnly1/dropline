from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dropline.provider_registry import (
    IMAGE_CAPABILITY,
    TEXT_CAPABILITY,
    ensure_provider_support,
    get_provider_api_key,
    get_provider_spec,
)
from tests.helpers import create_test_repo, load_test_settings


class ProviderRegistryTests(unittest.TestCase):
    def test_get_provider_spec_exposes_capabilities_and_required_paths(self) -> None:
        openai = get_provider_spec("openai")
        anthropic = get_provider_spec("anthropic")
        qwen = get_provider_spec("qwen")
        doubao = get_provider_spec("doubao")

        self.assertTrue(openai.supports(TEXT_CAPABILITY))
        self.assertTrue(openai.supports(IMAGE_CAPABILITY))
        self.assertEqual(openai.required_paths(IMAGE_CAPABILITY), ("text_model", "image_model"))
        self.assertTrue(anthropic.supports(TEXT_CAPABILITY))
        self.assertFalse(anthropic.supports(IMAGE_CAPABILITY))
        self.assertTrue(qwen.supports(IMAGE_CAPABILITY))
        self.assertTrue(doubao.supports(IMAGE_CAPABILITY))

    def test_get_provider_api_key_reads_provider_specific_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir).resolve()
            create_test_repo(root)
            settings = load_test_settings(root)

            self.assertEqual(get_provider_api_key(settings, "openai"), "test-openai-key")
            self.assertEqual(get_provider_api_key(settings, "grok"), "test-xai-key")
            self.assertEqual(get_provider_api_key(settings, "deepseek"), "test-deepseek-key")

    def test_ensure_provider_support_rejects_text_only_image_provider(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported image provider for llm.image"):
            ensure_provider_support(stage_name="llm.image", provider="deepseek", capability=IMAGE_CAPABILITY)


if __name__ == "__main__":
    unittest.main()
