from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING, Any


TEXT_CAPABILITY = "text"
IMAGE_CAPABILITY = "image"


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    label: str
    client_builder_path: str
    capabilities: frozenset[str]
    common_required_paths: tuple[str, ...] = ()
    image_required_paths: tuple[str, ...] = ()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    def required_paths(self, capability: str) -> tuple[str, ...]:
        if capability == IMAGE_CAPABILITY:
            return self.common_required_paths + self.image_required_paths
        return self.common_required_paths


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        name="openai",
        label="OpenAI",
        client_builder_path="dropline.llm:OpenAIBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY, IMAGE_CAPABILITY}),
        common_required_paths=("text_model",),
        image_required_paths=("image_model",),
    ),
    "gemini": ProviderSpec(
        name="gemini",
        label="Gemini",
        client_builder_path="dropline.llm:GeminiBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY, IMAGE_CAPABILITY}),
        common_required_paths=("text_model",),
        image_required_paths=("image_model",),
    ),
    "anthropic": ProviderSpec(
        name="anthropic",
        label="Anthropic",
        client_builder_path="dropline.llm:AnthropicBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY}),
        common_required_paths=("text_model",),
    ),
    "grok": ProviderSpec(
        name="grok",
        label="Grok",
        client_builder_path="dropline.llm:GrokBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY, IMAGE_CAPABILITY}),
        common_required_paths=("text_model",),
        image_required_paths=("image_model",),
    ),
    "qwen": ProviderSpec(
        name="qwen",
        label="Qwen",
        client_builder_path="dropline.llm:QwenBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY, IMAGE_CAPABILITY}),
        common_required_paths=("text_model",),
        image_required_paths=("image_model",),
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        label="DeepSeek",
        client_builder_path="dropline.llm:DeepSeekBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY}),
        common_required_paths=("text_model",),
    ),
    "glm": ProviderSpec(
        name="glm",
        label="GLM",
        client_builder_path="dropline.llm:GLMBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY}),
        common_required_paths=("text_model",),
    ),
    "doubao": ProviderSpec(
        name="doubao",
        label="Doubao",
        client_builder_path="dropline.llm:DoubaoBackedClient",
        capabilities=frozenset({TEXT_CAPABILITY, IMAGE_CAPABILITY}),
        common_required_paths=("text_model",),
        image_required_paths=("image_model",),
    ),
}


if TYPE_CHECKING:
    from .config import Settings


def normalize_provider_name(provider: str) -> str:
    return provider.strip().lower()


def get_provider_spec(provider: str) -> ProviderSpec:
    normalized = normalize_provider_name(provider)
    spec = PROVIDER_SPECS.get(normalized)
    if spec is None:
        raise ValueError(f"Unsupported provider: {provider}")
    return spec


def ensure_provider_support(*, stage_name: str, provider: str, capability: str) -> None:
    normalized = normalize_provider_name(provider)
    spec = PROVIDER_SPECS.get(normalized)
    if spec is None:
        raise ValueError(f"Unsupported provider for {stage_name}: {provider}")
    if spec.supports(capability):
        return
    if capability == IMAGE_CAPABILITY:
        raise ValueError(f"Unsupported image provider for {stage_name}: {provider}")
    raise ValueError(f"Unsupported provider for {stage_name}: {provider}")


def get_provider_api_key(settings: Settings, provider: str) -> str:
    spec = get_provider_spec(provider)
    provider_settings = settings.providers[spec.name]
    return provider_settings.api_key


def get_provider_text_model(settings: Settings, provider: str) -> str:
    spec = get_provider_spec(provider)
    provider_settings = settings.providers[spec.name]
    return provider_settings.text_model


def get_provider_image_model(settings: Settings, provider: str) -> str:
    spec = get_provider_spec(provider)
    if not spec.supports(IMAGE_CAPABILITY):
        raise ValueError(f"Unsupported image provider: {provider}")
    provider_settings = settings.providers[spec.name]
    if not provider_settings.image_model:
        raise ValueError(f"Missing image model for provider: {provider}")
    return provider_settings.image_model


def build_provider_client(settings: Settings, provider: str) -> Any:
    spec = get_provider_spec(provider)
    module_name, _, attr_name = spec.client_builder_path.partition(":")
    if not module_name or not attr_name:
        raise ValueError(f"Invalid provider builder path for {spec.name}: {spec.client_builder_path}")
    module = import_module(module_name)
    builder = getattr(module, attr_name)
    return builder(settings)
