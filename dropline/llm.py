from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Protocol
from urllib.request import Request, urlopen

from .config import Settings, current_character_name
from .llm_contracts import (
    CHARACTER_PROFILE_TASK,
    CHINESE_OUTPUT_LANGUAGE,
    ENGLISH_OUTPUT_LANGUAGE,
    RELATIONSHIP_PROFILE_TASK,
    STAGE1_TASK,
    StructuredTaskSpec,
    build_stage2_task,
    character_profile_payload,
    relationship_profile_payload,
)
from .models import (
    CharacterProfileDraft,
    GeneratedImage,
    RelationshipProfileDraft,
    SceneCard,
    Stage1Result,
    Stage2Result,
)
from .provider_registry import (
    IMAGE_CAPABILITY,
    TEXT_CAPABILITY,
    build_provider_client,
    ensure_provider_support,
    get_provider_api_key,
    get_provider_image_model,
)
from .utils import retry_call

LOGGER = logging.getLogger(__name__)


CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")

IMAGE_INSTRUCTIONS = "你只生成一张与用户提示一致的图片。不要输出额外解释。"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_DOWNLOAD_CHUNK_BYTES = 64 * 1024

LANGUAGE_REPAIR_INSTRUCTIONS = (
    "你是结构化 JSON 语言修复器。"
    "你会收到一个已经符合目标 schema 的 JSON 对象，但其中的自然语言内容没有严格使用目标语言。"
    "你只能改写语言，不得改变结构、字段名、字段数量、枚举值、Markdown 层级、事实含义、细节密度或 JSON 形状。"
    "必须把所有自然语言字段完整改写为目标语言，包括 scene_card 的全部字段。"
    "不要保留源语言片段。"
    "不要输出 JSON 以外的任何解释。"
)

LANGUAGE_REPAIR_INSTRUCTIONS_ENGLISH = (
    "You are a structured JSON language repairer. "
    "You will receive a JSON object that already conforms to the target schema, but its natural-language content does not strictly use the target language. "
    "You may only rewrite language; do not change structure, field names, field count, enum values, Markdown hierarchy, factual meaning, detail density, or JSON shape. "
    "Rewrite every natural-language field completely into the target language, including all scene_card fields. "
    "Do not keep source-language fragments. "
    "Do not output anything except JSON."
)


class AIClient(Protocol):
    def generate_stage1(self, payload: dict[str, Any]) -> Stage1Result:
        ...

    def generate_stage2(self, payload: dict[str, Any]) -> Stage2Result:
        ...

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        ...

    def generate_character_profile(
        self,
        *,
        work_title: str | None,
        character_name: str,
    ) -> CharacterProfileDraft:
        ...

    def generate_relationship_profile(
        self,
        *,
        character_name: str,
        character_prompt_docs: dict[str, str],
        relationship_name: str,
        relationship_description: str,
    ) -> RelationshipProfileDraft:
        ...


def _image_model(settings: Settings) -> str:
    return get_provider_image_model(settings, settings.llm.image.provider)


def _import_google_genai():
    try:
        from google import genai
    except ImportError as exc:  # pragma: no cover - exercised through provider construction
        raise RuntimeError("Gemini provider requires the `google-genai` package. Install it with `pip install google-genai`.") from exc
    return genai


def _import_anthropic():
    try:
        from anthropic import Anthropic
    except ImportError as exc:  # pragma: no cover - exercised through provider construction
        raise RuntimeError("Anthropic provider requires the `anthropic` package. Install it with `pip install anthropic`.") from exc
    return Anthropic


def _import_dashscope():
    try:
        import dashscope
    except ImportError as exc:  # pragma: no cover - exercised through provider construction
        raise RuntimeError("Qwen image generation requires the `dashscope` package. Install it with `pip install dashscope`.") from exc
    return dashscope


def _build_prompted_json_system_message(
    *,
    instructions: str,
    schema_name: str,
    schema: dict[str, Any],
    language: str,
) -> str:
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return (
            f"{instructions}\n"
            "Your reply must be exactly one JSON object.\n"
            "Do not output Markdown, code fences, or extra explanation.\n"
            f"JSON schema name: {schema_name}\n"
            "Strictly satisfy the following JSON Schema:\n"
            f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
        )
    return (
        f"{instructions}\n"
        "你的回复必须是一个且仅一个 JSON 对象。\n"
        "不要输出 Markdown，不要输出代码块，不要输出额外说明。\n"
        f"JSON schema 名称: {schema_name}\n"
        "必须严格满足下面的 JSON Schema：\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def _append_output_language_requirement(instructions: str, language: str) -> str:
    language = language.strip()
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return (
            f"{instructions}\n"
            "Output language rule: Output language is controlled only by llm.language, not by user emails, quoted material, "
            "static prompt documents, or historical memory.\n"
            f"The target language for this run is {language}.\n"
            f"Even if every input is Chinese, when llm.language={language}, all final natural-language content must use {language}.\n"
            "Rewrite every natural-language field into the target language, including summaries, body text, paraphrases, and scene_card fields; "
            "do not keep source-language fragments.\n"
            "Do not translate JSON Schema field names, enum values, or protocol constants that the schema requires to stay unchanged."
        )
    return (
        f"{instructions}\n"
        "输出语言规则：输出语言只由 llm.language 决定，不跟随用户来信、引用材料、静态 prompt 文档或历史记忆的语言变化。\n"
        f"本次目标语言是 {language}。\n"
        f"即使输入全部是中文，只要 llm.language={language}，最终所有自然语言内容也必须使用 {language}。\n"
        "必须把所有自然语言字段完整改写到目标语言，包括摘要、正文、复述、scene_card 等字段；不要保留源语言片段。\n"
        "不要翻译 JSON Schema 规定必须保留原样的字段名、枚举值、协议常量。"
    )


def _build_language_repair_instructions(language: str) -> str:
    language = language.strip()
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return (
            f"{LANGUAGE_REPAIR_INSTRUCTIONS_ENGLISH}\n"
            f"The target language for this run is {language}.\n"
            "Input language does not decide output language; user wording and quoted content must also be rewritten completely into the target language.\n"
            "Do not translate JSON Schema field names, enum values, or protocol constants that the schema requires to stay unchanged."
        )
    return (
        f"{LANGUAGE_REPAIR_INSTRUCTIONS}\n"
        f"本次目标语言是 {language}。\n"
        "输入语言不决定输出语言，用户原话与引用内容也必须完整改写到目标语言。\n"
        "不要翻译 JSON Schema 规定必须保留原样的字段名、枚举值、协议常量。"
    )


def _collect_translatable_fields(
    payload: Any,
    schema: dict[str, Any],
    *,
    path: str = "$",
) -> list[tuple[str, str]]:
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(payload, dict):
            return []
        fields: list[tuple[str, str]] = []
        properties = schema.get("properties", {})
        for key, value in payload.items():
            property_schema = properties.get(key)
            if isinstance(property_schema, dict):
                fields.extend(_collect_translatable_fields(value, property_schema, path=f"{path}.{key}"))
        return fields
    if schema_type == "array":
        if not isinstance(payload, list):
            return []
        item_schema = schema.get("items")
        if not isinstance(item_schema, dict):
            return []
        fields: list[tuple[str, str]] = []
        for index, item in enumerate(payload):
            fields.extend(_collect_translatable_fields(item, item_schema, path=f"{path}[{index}]"))
        return fields
    if schema_type == "string":
        if "enum" in schema or not isinstance(payload, str):
            return []
        return [(path, payload)]
    return []


def _english_field_violates_requirement(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    if CJK_PATTERN.search(stripped):
        return True
    letters = [char for char in stripped if char.isalpha()]
    if not letters:
        return False
    latin_letters = [char for char in letters if char.isascii()]
    return not latin_letters or len(latin_letters) * 2 < len(letters)


def _chinese_field_violates_requirement(value: str) -> bool:
    stripped = value.strip()
    if not stripped:
        return False
    cjk_chars = CJK_PATTERN.findall(stripped)
    if not cjk_chars:
        english_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", stripped)
        return len(english_words) >= 4
    latin_letters = [char for char in stripped if char.isascii() and char.isalpha()]
    return len(latin_letters) > len(cjk_chars) * 2


def _language_mismatches(
    payload: dict[str, Any],
    schema: dict[str, Any],
    *,
    language: str,
) -> list[tuple[str, str]]:
    if language not in {ENGLISH_OUTPUT_LANGUAGE, CHINESE_OUTPUT_LANGUAGE}:
        return []
    mismatches: list[tuple[str, str]] = []
    for path, value in _collect_translatable_fields(payload, schema):
        if language == ENGLISH_OUTPUT_LANGUAGE and _english_field_violates_requirement(value):
            mismatches.append((path, value))
        if language == CHINESE_OUTPUT_LANGUAGE and _chinese_field_violates_requirement(value):
            mismatches.append((path, value))
    return mismatches


def _language_repair_payload(
    *,
    language: str,
    task: StructuredTaskSpec,
    output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "target_language": language,
        "schema_name": task.schema_name,
        "rewrite_mode": "language_only",
        "current_output": output,
    }


def _image_instructions(language: str) -> str:
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return "Generate exactly one image that matches the prompt. Do not output any extra explanation."
    return IMAGE_INSTRUCTIONS


def _extract_openai_chat_text(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
            continue
        if getattr(item, "type", "") != "text":
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str) and text:
            chunks.append(text)
            continue
        value = getattr(text, "value", None)
        if isinstance(value, str) and value:
            chunks.append(value)
    return "".join(chunks)


def _strip_markdown_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return stripped


def _extract_first_json_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _load_best_effort_json_object(content: str, *, provider_label: str, schema_name: str) -> dict[str, Any]:
    raw = content.strip()
    candidates: list[str] = []
    for candidate in (
        raw,
        _strip_markdown_code_fence(raw),
        _extract_first_json_object(raw),
        _extract_first_json_object(_strip_markdown_code_fence(raw)),
    ):
        candidate = candidate.strip()
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    last_error: Exception | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        if isinstance(parsed, dict):
            return parsed
        raise RuntimeError(f"{provider_label} API returned non-object JSON for schema '{schema_name}'.")

    snippet = raw[:200].replace("\n", "\\n")
    raise RuntimeError(
        f"{provider_label} API returned invalid JSON for schema '{schema_name}'. First 200 chars: {snippet}"
    ) from last_error


class TextOnlyBackedClient:
    provider_label = "Provider"

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        raise NotImplementedError(f"{self.provider_label} provider does not support image generation.")


class StructuredJSONClient(TextOnlyBackedClient):
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def generate_stage1(self, payload: dict[str, Any]) -> Stage1Result:
        return self._run_structured_task(STAGE1_TASK, payload)

    def generate_stage2(self, payload: dict[str, Any]) -> Stage2Result:
        return self._run_structured_task(build_stage2_task(self.settings), payload)

    def generate_character_profile(
        self,
        *,
        work_title: str | None,
        character_name: str,
    ) -> CharacterProfileDraft:
        return self._run_structured_task(
            CHARACTER_PROFILE_TASK,
            character_profile_payload(
                work_title=work_title,
                character_name=character_name,
            ),
        )

    def generate_relationship_profile(
        self,
        *,
        character_name: str,
        character_prompt_docs: dict[str, str],
        relationship_name: str,
        relationship_description: str,
    ) -> RelationshipProfileDraft:
        return self._run_structured_task(
            RELATIONSHIP_PROFILE_TASK,
            relationship_profile_payload(
                character_name=character_name,
                character_prompt_docs=character_prompt_docs,
                relationship_name=relationship_name,
                relationship_description=relationship_description,
            ),
        )

    def _run_structured_task(self, task: StructuredTaskSpec, payload: dict[str, Any]) -> Any:
        instructions = _append_output_language_requirement(task.instructions_for(self.settings), self.settings.llm.language)
        raw = self._complete_json(
            model=task.model_for(self.settings),
            instructions=instructions,
            payload=payload,
            schema_name=task.schema_name,
            schema=task.schema,
        )
        raw = self._repair_language_mismatch(
            task=task,
            model=task.model_for(self.settings),
            output=raw,
        )
        return task.parse_result(raw)

    def _repair_language_mismatch(
        self,
        *,
        task: StructuredTaskSpec,
        model: str,
        output: dict[str, Any],
    ) -> dict[str, Any]:
        mismatches = _language_mismatches(output, task.schema, language=self.settings.llm.language)
        if not mismatches:
            return output
        sample = "; ".join(f"{path}={value[:40]!r}" for path, value in mismatches[:3])
        LOGGER.warning(
            "Structured output for %s did not match target language %s; attempting one repair pass. Sample mismatches: %s",
            task.schema_name,
            self.settings.llm.language,
            sample,
        )
        repaired = self._complete_json(
            model=model,
            instructions=_build_language_repair_instructions(self.settings.llm.language),
            payload=_language_repair_payload(
                language=self.settings.llm.language,
                task=task,
                output=output,
            ),
            schema_name=task.schema_name,
            schema=task.schema,
        )
        remaining = _language_mismatches(repaired, task.schema, language=self.settings.llm.language)
        if remaining:
            sample = "; ".join(f"{path}={value[:40]!r}" for path, value in remaining[:3])
            raise RuntimeError(
                f"Structured output for schema '{task.schema_name}' could not be repaired to target language "
                f"'{self.settings.llm.language}'. Sample mismatches: {sample}"
            )
        return repaired

    def _complete_json(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError


class DisabledImageClient:
    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        raise NotImplementedError("Image generation is disabled in config.")


def _scene_card_prompt(settings: Settings, scene_card: SceneCard) -> str:
    return render_scene_card_prompt(
        current_character_name(settings),
        scene_card,
        language=settings.llm.language,
    )


def _get_mapping_value(payload: Any, key: str, default: Any = None) -> Any:
    if isinstance(payload, dict):
        return payload.get(key, default)
    return getattr(payload, key, default)


def _download_image_bytes(image_url: str, *, provider_label: str) -> bytes:
    def fetch() -> bytes:
        request = Request(image_url, headers={"User-Agent": "dropline/0.1"})
        with urlopen(request, timeout=30) as response:
            content_type = (response.headers.get("Content-Type", "") or "").split(";", 1)[0].strip().lower()
            if not content_type:
                raise RuntimeError(f"{provider_label} image download returned missing Content-Type.")
            if not content_type.startswith("image/"):
                raise RuntimeError(
                    f"{provider_label} image download returned non-image Content-Type: {content_type}."
                )

            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    expected_size = int(content_length)
                except ValueError as exc:
                    raise RuntimeError(
                        f"{provider_label} image download returned invalid Content-Length: {content_length}."
                    ) from exc
                if expected_size > MAX_IMAGE_BYTES:
                    raise RuntimeError(
                        f"{provider_label} image download exceeds maximum size of {MAX_IMAGE_BYTES} bytes."
                    )

            chunks: list[bytes] = []
            total_bytes = 0
            while True:
                chunk = response.read(IMAGE_DOWNLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_IMAGE_BYTES:
                    raise RuntimeError(
                        f"{provider_label} image download exceeds maximum size of {MAX_IMAGE_BYTES} bytes."
                    )
                chunks.append(chunk)

        image_bytes = b"".join(chunks)
        if not image_bytes:
            raise RuntimeError(f"{provider_label} image download returned empty bytes.")
        return image_bytes

    return retry_call(
        fetch,
        operation_name=f"{provider_label} image download",
        logger=LOGGER,
    )


def _extract_qwen_image_url(response: Any) -> str:
    output = _get_mapping_value(response, "output")
    choices = _get_mapping_value(output, "choices") or _get_mapping_value(response, "choices") or []
    for choice in choices:
        message = _get_mapping_value(choice, "message")
        if message is None:
            continue
        content = _get_mapping_value(message, "content") or []
        for item in content:
            url = _extract_qwen_content_image_url(item)
            if url:
                return url
    return ""


def _extract_qwen_content_image_url(item: Any) -> str:
    if isinstance(item, str) and item.strip():
        return item.strip() if item.strip().startswith("http") else ""
    for key in ("image", "url", "image_url"):
        value = _get_mapping_value(item, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None:
            nested_url = _extract_qwen_content_image_url(value)
            if nested_url:
                return nested_url
    return ""


class OpenAIBackedClient(StructuredJSONClient):
    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        super().__init__(settings)
        self.client = OpenAI(api_key=get_provider_api_key(settings, "openai") or None)

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        prompt_text = _scene_card_prompt(self.settings, scene_card)
        response = retry_call(
            lambda: self.client.images.generate(
                model=_image_model(self.settings),
                prompt=prompt_text,
                size="1536x1024",
                output_format="png",
            ),
            operation_name="OpenAI image generation",
            logger=LOGGER,
        )
        image_payload = getattr(response.data[0], "b64_json", None) if getattr(response, "data", None) else None
        if not image_payload:
            raise RuntimeError("OpenAI image generation returned no base64 payload.")
        return GeneratedImage(prompt_text=prompt_text, image_bytes=base64.b64decode(image_payload))

    def _complete_json(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = retry_call(
            lambda: self.client.responses.create(
                model=model,
                instructions=instructions,
                input=json.dumps(payload, ensure_ascii=False, indent=2),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name,
                        "schema": schema,
                        "strict": True,
                    }
                },
            ),
            operation_name=f"OpenAI structured response ({schema_name})",
            logger=LOGGER,
        )
        content = (response.output_text or "").strip()
        if not content:
            raise RuntimeError(f"Responses API returned empty output for schema '{schema_name}'.")
        return json.loads(content)


class GeminiBackedClient(StructuredJSONClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        genai = _import_google_genai()
        self.client = genai.Client(api_key=get_provider_api_key(settings, "gemini") or None)

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        prompt_text = _scene_card_prompt(self.settings, scene_card)
        response = retry_call(
            lambda: self.client.models.generate_content(
                model=_image_model(self.settings),
                contents=prompt_text,
                config={
                    "response_modalities": ["IMAGE"],
                    "image_config": {
                        "aspect_ratio": "3:2",
                        "image_size": "2K",
                    },
                    "system_instruction": _image_instructions(self.settings.llm.language),
                },
            ),
            operation_name="Gemini image generation",
            logger=LOGGER,
        )
        image_bytes = _extract_gemini_image_bytes(response)
        if not image_bytes:
            raise RuntimeError("Gemini image generation returned no image bytes.")
        return GeneratedImage(prompt_text=prompt_text, image_bytes=image_bytes)

    def _complete_json(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = retry_call(
            lambda: self.client.models.generate_content(
                model=model,
                contents=json.dumps(payload, ensure_ascii=False, indent=2),
                config={
                    "system_instruction": instructions,
                    "response_mime_type": "application/json",
                    "response_json_schema": schema,
                },
            ),
            operation_name=f"Gemini structured response ({schema_name})",
            logger=LOGGER,
        )
        content = _extract_gemini_text(response).strip()
        if not content:
            raise RuntimeError(f"Gemini API returned empty output for schema '{schema_name}'.")
        return json.loads(content)


class AnthropicBackedClient(StructuredJSONClient):
    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        anthropic_cls = _import_anthropic()
        self.client = anthropic_cls(api_key=get_provider_api_key(settings, "anthropic") or None)

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        raise NotImplementedError("Anthropic provider does not support image generation.")

    def _complete_json(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = retry_call(
            lambda: self.client.messages.create(
                model=model,
                max_tokens=4096,
                system=instructions,
                messages=[
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, indent=2),
                    }
                ],
                output_config={
                    "format": {
                        "type": "json_schema",
                        "schema": schema,
                    }
                },
            ),
            operation_name=f"Anthropic structured response ({schema_name})",
            logger=LOGGER,
        )
        content = _extract_anthropic_text(response).strip()
        if not content:
            raise RuntimeError(f"Anthropic API returned empty output for schema '{schema_name}'.")
        return json.loads(content)


class PromptedOpenAICompatibleTextClient(StructuredJSONClient):
    provider_label = "Provider"
    base_url = ""

    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        super().__init__(settings)
        self.client = OpenAI(api_key=self._api_key() or None, base_url=self.base_url)

    def _api_key(self) -> str:
        raise NotImplementedError

    def _structured_json_kwargs(self) -> dict[str, Any]:
        return {}

    def _complete_json(
        self,
        *,
        model: str,
        instructions: str,
        payload: dict[str, Any],
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        response = retry_call(
            lambda: self.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": _build_prompted_json_system_message(
                            instructions=instructions,
                            schema_name=schema_name,
                            schema=schema,
                            language=self.settings.llm.language,
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, indent=2),
                    },
                ],
                **self._structured_json_kwargs(),
            ),
            operation_name=f"{self.provider_label} structured response ({schema_name})",
            logger=LOGGER,
        )
        content = _extract_openai_chat_text(response).strip()
        if not content:
            raise RuntimeError(f"{self.provider_label} API returned empty output for schema '{schema_name}'.")
        return _load_best_effort_json_object(content, provider_label=self.provider_label, schema_name=schema_name)


class QwenBackedClient(PromptedOpenAICompatibleTextClient):
    provider_label = "Qwen"
    base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def _api_key(self) -> str:
        return get_provider_api_key(self.settings, "qwen")

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        dashscope = _import_dashscope()
        dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"
        prompt_text = _scene_card_prompt(self.settings, scene_card)
        response = retry_call(
            lambda: dashscope.MultiModalConversation.call(
                api_key=get_provider_api_key(self.settings, "qwen") or None,
                model=_image_model(self.settings),
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt_text}],
                    }
                ],
                result_format="message",
                stream=False,
                watermark=False,
                prompt_extend=True,
                size="2048*2048",
            ),
            operation_name="Qwen image generation",
            logger=LOGGER,
        )
        status_code = _get_mapping_value(response, "status_code")
        if status_code != 200:
            error_code = _get_mapping_value(response, "code", "unknown")
            message = str(_get_mapping_value(response, "message", "")).strip()
            details = f"{error_code}: {message}" if message else str(error_code)
            raise RuntimeError(f"Qwen image generation failed with status {status_code}: {details}")
        image_url = _extract_qwen_image_url(response)
        if not image_url:
            raise RuntimeError("Qwen image generation returned no image URL.")
        return GeneratedImage(
            prompt_text=prompt_text,
            image_bytes=_download_image_bytes(image_url, provider_label="Qwen"),
        )


class DeepSeekBackedClient(PromptedOpenAICompatibleTextClient):
    provider_label = "DeepSeek"
    base_url = "https://api.deepseek.com"

    def _api_key(self) -> str:
        return get_provider_api_key(self.settings, "deepseek")

    def _structured_json_kwargs(self) -> dict[str, Any]:
        return {"response_format": {"type": "json_object"}}


class GLMBackedClient(PromptedOpenAICompatibleTextClient):
    provider_label = "GLM"
    base_url = "https://open.bigmodel.cn/api/paas/v4"

    def _api_key(self) -> str:
        return get_provider_api_key(self.settings, "glm")


class DoubaoBackedClient(PromptedOpenAICompatibleTextClient):
    provider_label = "Doubao"
    base_url = "https://ark.cn-beijing.volces.com/api/v3"

    def _api_key(self) -> str:
        return get_provider_api_key(self.settings, "doubao")

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        prompt_text = _scene_card_prompt(self.settings, scene_card)
        response = retry_call(
            lambda: self.client.images.generate(
                model=_image_model(self.settings),
                prompt=prompt_text,
                size="2K",
                output_format="png",
                response_format="url",
                extra_body={"watermark": False},
            ),
            operation_name="Doubao image generation",
            logger=LOGGER,
        )
        image_url = _get_mapping_value((getattr(response, "data", None) or [None])[0], "url", "")
        if not image_url:
            raise RuntimeError("Doubao image generation returned no image URL.")
        return GeneratedImage(
            prompt_text=prompt_text,
            image_bytes=_download_image_bytes(image_url, provider_label="Doubao"),
        )


class GrokBackedClient(OpenAIBackedClient):
    def __init__(self, settings: Settings) -> None:
        from openai import OpenAI

        StructuredJSONClient.__init__(self, settings)
        self.client = OpenAI(api_key=get_provider_api_key(settings, "grok") or None, base_url="https://api.x.ai/v1")

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        prompt_text = _scene_card_prompt(self.settings, scene_card)
        response = retry_call(
            lambda: self.client.images.generate(
                model=_image_model(self.settings),
                prompt=prompt_text,
                response_format="b64_json",
            ),
            operation_name="Grok image generation",
            logger=LOGGER,
        )
        image_payload = response.data[0].b64_json if getattr(response, "data", None) else None
        if not image_payload:
            raise RuntimeError("Grok image generation returned no base64 payload.")
        return GeneratedImage(prompt_text=prompt_text, image_bytes=base64.b64decode(image_payload))


class RoutedAIClient:
    def __init__(self, *, stage1_client: AIClient, stage2_client: AIClient, image_client: AIClient) -> None:
        self.stage1_client = stage1_client
        self.stage2_client = stage2_client
        self.image_client = image_client

    def generate_stage1(self, payload: dict[str, Any]) -> Stage1Result:
        return self.stage1_client.generate_stage1(payload)

    def generate_stage2(self, payload: dict[str, Any]) -> Stage2Result:
        return self.stage2_client.generate_stage2(payload)

    def generate_image(self, scene_card: SceneCard) -> GeneratedImage:
        return self.image_client.generate_image(scene_card)

    def generate_character_profile(
        self,
        *,
        work_title: str | None,
        character_name: str,
    ) -> CharacterProfileDraft:
        return self.stage2_client.generate_character_profile(
            work_title=work_title,
            character_name=character_name,
        )

    def generate_relationship_profile(
        self,
        *,
        character_name: str,
        character_prompt_docs: dict[str, str],
        relationship_name: str,
        relationship_description: str,
    ) -> RelationshipProfileDraft:
        return self.stage2_client.generate_relationship_profile(
            character_name=character_name,
            character_prompt_docs=character_prompt_docs,
            relationship_name=relationship_name,
            relationship_description=relationship_description,
        )


def build_ai_client(settings: Settings) -> AIClient:
    stage_bindings = [
        ("llm.stage1", settings.llm.stage1.provider, TEXT_CAPABILITY),
        ("llm.stage2", settings.llm.stage2.provider, TEXT_CAPABILITY),
    ]
    if settings.llm.image.enabled:
        stage_bindings.append(("llm.image", settings.llm.image.provider, IMAGE_CAPABILITY))

    for stage_name, provider, capability in stage_bindings:
        ensure_provider_support(stage_name=stage_name, provider=provider, capability=capability)

    provider_clients: dict[str, AIClient] = {}

    def get_client(provider: str) -> AIClient:
        normalized = provider.strip().lower()
        if normalized not in provider_clients:
            provider_clients[normalized] = build_provider_client(settings, normalized)
        return provider_clients[normalized]

    return RoutedAIClient(
        stage1_client=get_client(settings.llm.stage1.provider),
        stage2_client=get_client(settings.llm.stage2.provider),
        image_client=get_client(settings.llm.image.provider) if settings.llm.image.enabled else DisabledImageClient(),
    )


def build_profile_generation_client(settings: Settings) -> AIClient:
    ensure_provider_support(
        stage_name="llm.stage2",
        provider=settings.llm.stage2.provider,
        capability=TEXT_CAPABILITY,
    )
    client = build_provider_client(settings, settings.llm.stage2.provider.strip().lower())
    return RoutedAIClient(stage1_client=client, stage2_client=client, image_client=client)


def _extract_gemini_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text
    parts = getattr(response, "parts", None) or []
    texts = [str(part.text) for part in parts if getattr(part, "text", None)]
    return "".join(texts)


def _extract_gemini_image_bytes(response: Any) -> bytes:
    parts = getattr(response, "parts", None)
    if parts is None:
        parts = []
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            parts.extend(getattr(content, "parts", []) or [])
    for part in parts:
        inline_data = getattr(part, "inline_data", None)
        if inline_data is None:
            continue
        data = getattr(inline_data, "data", None)
        if isinstance(data, bytes):
            return data
        if isinstance(data, bytearray):
            return bytes(data)
        if isinstance(data, memoryview):
            return data.tobytes()
        if isinstance(data, str) and data:
            return base64.b64decode(data)
    return b""


def _extract_anthropic_text(response: Any) -> str:
    chunks: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text" and getattr(block, "text", None):
            chunks.append(str(block.text))
    return "".join(chunks)


def render_scene_card_prompt(character_name: str, scene_card: SceneCard, *, language: str = "简体中文") -> str:
    if language == ENGLISH_OUTPUT_LANGUAGE:
        return (
            f"Character: {character_name}. "
            f"Visual style: {scene_card.visual_style}. "
            f"Location: {scene_card.location}. "
            f"Action: {scene_card.action}. "
            f"Key object: {scene_card.key_object}. "
            f"Time: {scene_card.time}. "
            f"Mood: {scene_card.mood}. "
            f"Camera distance: {scene_card.camera_distance}. "
            "Do not include any visible text in the image. Use a cinematic composition and keep the scene coherent; "
            "for example, do not place modern machines in an ancient setting. "
            "Avoid making this image highly similar to recent generated images in location, time, action, key object, "
            "and camera distance."
        )
    return (
        f"{character_name}，画面风格：{scene_card.visual_style}，场景地点：{scene_card.location}，"
        f"人物动作：{scene_card.action}，关键物件：{scene_card.key_object}，"
        f"时间：{scene_card.time}，氛围：{scene_card.mood}，镜头距离：{scene_card.camera_distance}，"
        "画面中不要出现文字，构图具有电影感，注意画面的合理性，比如不要在古代场景中出现现代机器，"
        "同时避免与最近生成的图像在地点、时段、动作、关键物件和镜头距离上高度相似"
    )
