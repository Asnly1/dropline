from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Sequence

from .config import Settings, load_settings, validate_live_settings
from .documents import load_json_file, read_optional_text, read_required_text
from .llm import AIClient, build_ai_client
from .llm_contracts import ENGLISH_OUTPUT_LANGUAGE
from .mail import (
    IMAPInboundClient,
    InboundMailClient,
    OutboundMailClient,
    SMTPOutboundClient,
    compose_email,
    normalize_inbound_message,
)
from .memory import MemoryManager
from .models import ModelValidationError, NormalizedInboundMessage, RunOutcome, Stage1Result, Stage2Result
from .models import StoredOutboundMessage
from .storage import OUTBOUND_PENDING, StateStore
from .utils import hash_text, new_run_id, now_in_timezone


LOGGER = logging.getLogger(__name__)

RECENT_EPISODE_LOOKBACK_DAYS = 7
RECENT_SCENE_CARD_LIMIT = 7


@dataclass(slots=True)
class PipelineDependencies:
    inbound_client: InboundMailClient
    outbound_client: OutboundMailClient
    ai_client: AIClient
    state_store: StateStore
    memory_manager: MemoryManager


@dataclass(slots=True)
class RunContext:
    now: datetime
    run_id: str
    mode: str = "pending"
    batch_id: str = ""
    inbound_messages: list[NormalizedInboundMessage] = field(default_factory=list)
    highest_seen_uid: int = 0
    current_stage: str = "pipeline"
    stage1_path: Path | None = None
    stage2_path: Path | None = None
    image_generated: bool = False
    image_path: Path | None = None
    outbound_message_id: str | None = None


class PipelineService:
    def __init__(self, settings: Settings, dependencies: PipelineDependencies | None = None) -> None:
        self.settings = settings
        self.dependencies = dependencies or self._default_dependencies(settings)

    def run_once(self) -> RunOutcome:
        try:
            context = self._prepare_run()
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Pipeline run failed during preparation")
            return RunOutcome(
                success=False,
                run_id="",
                mode="pending",
                batch_id="",
                error_stage="prepare",
                message=str(exc),
            )
        try:
            self._log_step(context.run_id, 1, 8, "Waiting for run lock.")
            with self.dependencies.state_store.acquire_lock():
                self._log_step(context.run_id, 1, 8, "Run lock acquired.")
                self._pull_inbound_for_run(context)
                self._resolve_run_mode(context)
                self._validate_inbound_batch(context)
                existing_outbound = self.dependencies.state_store.get_outbound_message_for_batch(context.batch_id)
                if existing_outbound is not None:
                    if existing_outbound.delivery_status == OUTBOUND_PENDING:
                        return self._retry_pending_outbound_run(context, existing_outbound)
                    return self._finish_existing_outbound_run(context, existing_outbound)

                stage1_result = self._run_stage1(context)
                stage2_result = self._run_stage2(context, stage1_result)
                self._maybe_generate_image(context, stage2_result)
                self._send_outbound(context, stage2_result)
                return self._finish_successful_run(context, stage2_result)
        except Exception as exc:  # noqa: BLE001
            return self._finish_failed_run(context, exc)

    def _prepare_run(self) -> RunContext:
        self.dependencies.state_store.initialize()
        self.dependencies.memory_manager.bootstrap()
        now = now_in_timezone(self.settings.project.timezone)
        run_id = new_run_id(now)
        self.dependencies.state_store.start_run(run_id, started_at=now.isoformat())
        self._log_step(run_id, 0, 8, "Pipeline run started.", timezone=self.settings.project.timezone)
        return RunContext(now=now, run_id=run_id)

    def _pull_inbound_for_run(self, context: RunContext) -> None:
        if self._is_first_run_bootstrap():
            self._log_step(context.run_id, 2, 8, "First run detected; skipping inbox scan and forcing initiative mode.")
            return
        context.current_stage = "imap"
        self._log_step(context.run_id, 2, 8, "Pulling inbound mail.")
        context.inbound_messages, context.highest_seen_uid = self._pull_and_store_inbound(
            run_id=context.run_id,
            now=context.now,
        )
        self._log_step(
            context.run_id,
            2,
            8,
            "Inbound mail pull complete.",
            inbound_count=len(context.inbound_messages),
            highest_seen_uid=context.highest_seen_uid,
        )
        if context.inbound_messages:
            self._log_step(context.run_id, 2, 8, "Archiving normalized inbound messages.")
            self.dependencies.state_store.save_normalized_inbound_messages(
                context.run_id,
                context.now,
                context.inbound_messages,
            )

    def _resolve_run_mode(self, context: RunContext) -> None:
        context.mode = "reply" if context.inbound_messages else "initiative"
        context.batch_id = self._compute_batch_id(
            mode=context.mode,
            inbound_messages=context.inbound_messages,
            now=context.now,
        )
        self.dependencies.state_store.update_run_context(context.run_id, mode=context.mode, batch_id=context.batch_id)
        self._log_step(context.run_id, 3, 8, "Run mode resolved.", mode=context.mode, batch_id=context.batch_id)

    def _finish_existing_outbound_run(
        self,
        context: RunContext,
        existing_outbound: StoredOutboundMessage,
    ) -> RunOutcome:
        self._log_step(
            context.run_id,
            3,
            8,
            "Recorded outbound message already exists for batch; finalizing stored delivery.",
            outbound_message_id=existing_outbound.message_id,
        )
        stage2_result = self._load_stage2_for_existing_outbound(existing_outbound)
        self.dependencies.memory_manager.write_character_side(
            stage2_result=stage2_result,
            batch_id=context.batch_id,
            now=context.now,
        )
        self._mark_inbound_consumed(context)
        self.dependencies.state_store.finish_run(
            context.run_id,
            status="success",
            finished_at=context.now.isoformat(),
        )
        return RunOutcome(
            success=True,
            run_id=context.run_id,
            mode=context.mode,
            batch_id=context.batch_id,
            outbound_message_id=existing_outbound.message_id,
            image_generated=bool(existing_outbound.image_path),
        )

    def _retry_pending_outbound_run(
        self,
        context: RunContext,
        existing_outbound: StoredOutboundMessage,
    ) -> RunOutcome:
        self._log_step(
            context.run_id,
            7,
            8,
            "Pending outbound message already exists for batch; resending archived raw email.",
            outbound_message_id=existing_outbound.message_id,
        )
        context.current_stage = "smtp"
        context.outbound_message_id = existing_outbound.message_id
        email_message = BytesParser(policy=policy.default).parsebytes(Path(existing_outbound.raw_path).read_bytes())
        self.dependencies.outbound_client.send(email_message)
        self.dependencies.state_store.mark_outbound_message_sent(
            message_id=existing_outbound.message_id,
            sent_at=context.now.isoformat(),
        )
        return self._finish_existing_outbound_run(context, existing_outbound)

    def _run_stage1(self, context: RunContext) -> Stage1Result:
        context.current_stage = "stage1"
        stage1_payload = self._build_stage1_payload(
            mode=context.mode,
            inbound_messages=context.inbound_messages,
            now=context.now,
        )
        self._log_step(context.run_id, 4, 8, "Calling Stage 1.")
        stage1_result = self.dependencies.ai_client.generate_stage1(stage1_payload)
        context.stage1_path = self.dependencies.state_store.save_stage_json(
            context.run_id,
            context.now,
            "stage1.json",
            stage1_result.to_dict(),
        )
        self._log_step(context.run_id, 4, 8, "Stage 1 completed.", stage1_path=context.stage1_path.name)
        if context.inbound_messages:
            self._log_step(context.run_id, 4, 8, "Writing user-side memory.")
            self.dependencies.memory_manager.write_user_side(
                stage1_result=stage1_result,
                batch_id=context.batch_id,
                now=context.now,
                source_message_ids=[item.message_id for item in context.inbound_messages],
                source_dates=[item.received_at[:10] for item in context.inbound_messages if item.received_at],
            )
        return stage1_result

    def _run_stage2(self, context: RunContext, stage1_result: Stage1Result) -> Stage2Result:
        context.current_stage = "stage2"
        stage2_payload = self._build_stage2_payload(
            mode=context.mode,
            inbound_messages=context.inbound_messages,
            stage1_result=stage1_result,
            now=context.now,
        )
        self._log_step(context.run_id, 5, 8, "Calling Stage 2.")
        stage2_result = self.dependencies.ai_client.generate_stage2(stage2_payload)
        context.current_stage = "validation"
        self._log_step(context.run_id, 5, 8, "Validating Stage 2 output.")
        self._validate_stage2(stage2_result)
        context.stage2_path = self.dependencies.state_store.save_stage_json(
            context.run_id,
            context.now,
            "stage2.json",
            stage2_result.to_dict(),
        )
        self._log_step(context.run_id, 5, 8, "Stage 2 completed.", stage2_path=context.stage2_path.name)
        return stage2_result

    def _maybe_generate_image(self, context: RunContext, stage2_result: Stage2Result) -> None:
        if not self.settings.llm.image.enabled:
            self._log_step(context.run_id, 6, 8, "Image generation disabled by config; sending text-only email.")
            return
        context.current_stage = "image"
        self._log_step(context.run_id, 6, 8, "Generating image from scene card.")
        generated_image = self.dependencies.ai_client.generate_image(stage2_result.scene_card)
        context.image_path = self.dependencies.state_store.save_generated_image(context.run_id, context.now, generated_image)
        self.dependencies.state_store.record_asset(
            asset_id=hash_text(context.run_id, prefix="asset-"),
            run_id=context.run_id,
            image_path=context.image_path,
            scene_card=stage2_result.scene_card.to_dict(),
            prompt_text=generated_image.prompt_text,
            created_at=context.now.isoformat(),
        )
        context.image_generated = True
        self._log_step(context.run_id, 6, 8, "Image generation completed.", image_path=context.image_path.name)

    def _send_outbound(self, context: RunContext, stage2_result: Stage2Result) -> None:
        reply_to_message_id = context.inbound_messages[-1].message_id if context.inbound_messages else None
        context.current_stage = "smtp"
        self._log_step(context.run_id, 7, 8, "Composing outbound email.", reply_mode=(context.mode == "reply"))
        email_message = compose_email(
            self.settings,
            subject=stage2_result.subject,
            body_text=stage2_result.body,
            image_path=context.image_path,
            reply_to_message_id=reply_to_message_id,
            mode=context.mode,
        )
        context.outbound_message_id = email_message["Message-ID"]
        if context.outbound_message_id is None:
            raise RuntimeError("Composed outbound email is missing Message-ID.")
        self._log_step(context.run_id, 7, 8, "Sending outbound email.")
        raw_path = self.dependencies.state_store.save_outbound_raw(
            context.run_id,
            email_message.as_bytes(),
            sent_at=context.now,
        )
        self.dependencies.state_store.record_pending_outbound_message(
            message_id=context.outbound_message_id,
            sent_at=context.now.isoformat(),
            to_email=self.settings.mail.primary_user_recipient,
            subject=email_message["Subject"],
            raw_path=raw_path,
            image_path=context.image_path,
            run_id=context.run_id,
            batch_id=context.batch_id,
        )
        self.dependencies.outbound_client.send(email_message)
        self.dependencies.state_store.mark_outbound_message_sent(
            message_id=context.outbound_message_id,
            sent_at=context.now.isoformat(),
        )
        self._log_step(context.run_id, 7, 8, "Outbound email sent.", outbound_message_id=context.outbound_message_id)

    def _finish_successful_run(self, context: RunContext, stage2_result: Stage2Result) -> RunOutcome:
        context.current_stage = "pipeline"
        self._log_step(context.run_id, 8, 8, "Writing character-side memory and updating state.")
        self.dependencies.memory_manager.write_character_side(
            stage2_result=stage2_result,
            batch_id=context.batch_id,
            now=context.now,
        )
        self._mark_inbound_consumed(context)
        self.dependencies.state_store.finish_run(
            context.run_id,
            status="success",
            finished_at=context.now.isoformat(),
            stage1_path=str(context.stage1_path) if context.stage1_path else None,
            stage2_path=str(context.stage2_path) if context.stage2_path else None,
        )
        self._log_step(
            context.run_id,
            8,
            8,
            "Pipeline run succeeded.",
            archive_dir=self.settings.documents.session_archive_dir / context.now.date().isoformat() / context.run_id,
        )
        return RunOutcome(
            success=True,
            run_id=context.run_id,
            mode=context.mode,
            batch_id=context.batch_id,
            outbound_message_id=context.outbound_message_id,
            image_generated=context.image_generated,
        )

    def _finish_failed_run(self, context: RunContext, exc: Exception) -> RunOutcome:
        LOGGER.exception("Pipeline run failed")
        error_stage = _classify_error(exc, current_stage=context.current_stage)
        finished_at = now_in_timezone(self.settings.project.timezone).isoformat()
        self.dependencies.state_store.finish_run(
            context.run_id,
            status="failed",
            finished_at=finished_at,
            error_stage=error_stage,
            stage1_path=str(context.stage1_path) if context.stage1_path else None,
            stage2_path=str(context.stage2_path) if context.stage2_path else None,
        )
        return RunOutcome(
            success=False,
            run_id=context.run_id,
            mode=context.mode,
            batch_id=context.batch_id,
            error_stage=error_stage,
            message=str(exc),
            outbound_message_id=context.outbound_message_id,
            image_generated=context.image_generated,
        )

    def _mark_inbound_consumed(self, context: RunContext) -> None:
        consumed_uids = [item.imap_uid for item in context.inbound_messages]
        self.dependencies.state_store.mark_inbound_consumed(
            consumed_uids,
            batch_id=context.batch_id,
        )
        highest_consumed_uid = max(consumed_uids, default=0)
        if highest_consumed_uid > 0:
            current_uid = self.dependencies.state_store.get_last_consumed_imap_uid()
            self.dependencies.state_store.set_last_consumed_imap_uid(max(current_uid, highest_consumed_uid))

    def _log_step(self, run_id: str, step: int, total: int, message: str, **fields: object) -> None:
        suffix = ""
        if fields:
            suffix = " " + ", ".join(f"{key}={value}" for key, value in fields.items())
        LOGGER.info("[%s] [%s/%s] %s%s", run_id, step, total, message, suffix)

    def _pull_and_store_inbound(self, *, run_id: str, now: datetime) -> tuple[list[NormalizedInboundMessage], int]:
        since_uid = self.dependencies.state_store.get_last_seen_imap_uid()
        received_since = None
        first_outbound_sent_at = None
        if since_uid <= 0:
            first_outbound_sent_at = self.dependencies.state_store.get_first_outbound_sent_at()
            if first_outbound_sent_at is not None:
                received_since = first_outbound_sent_at.date()
        raw_messages, highest_seen_uid = self.dependencies.inbound_client.fetch_since(
            since_uid,
            0,
            self.settings.mail.allowlist_senders,
            received_since,
        )
        fresh_messages_by_uid: dict[int, NormalizedInboundMessage] = {}
        for raw_message in raw_messages:
            normalized = normalize_inbound_message(raw_message)
            if self.settings.mail.allowlist_senders and normalized.from_email not in self.settings.mail.allowlist_senders:
                LOGGER.info(
                    "[%s] Skipping inbound message from non-allowlisted sender: %s",
                    run_id,
                    normalized.from_email,
                )
                continue
            if _is_at_or_before_cutoff(normalized.received_at, first_outbound_sent_at):
                LOGGER.info(
                    "[%s] Skipping first-run backlog message at or before first outbound: uid=%s received_at=%s",
                    run_id,
                    normalized.imap_uid,
                    normalized.received_at,
                )
                continue
            raw_path = self.dependencies.state_store.save_inbound_artifacts(run_id, now, normalized, raw_message.raw_bytes)
            self.dependencies.state_store.upsert_inbound_message(
                normalized,
                raw_path=raw_path,
            )
            fresh_messages_by_uid[normalized.imap_uid] = normalized
        if highest_seen_uid > 0:
            current_seen_uid = self.dependencies.state_store.get_last_seen_imap_uid()
            self.dependencies.state_store.set_last_seen_imap_uid(max(current_seen_uid, highest_seen_uid))
        stored = self.dependencies.state_store.list_unconsumed_inbound()
        messages = [
            fresh_messages_by_uid.get(
                item.imap_uid,
                NormalizedInboundMessage(
                    message_id=item.message_id,
                    imap_uid=item.imap_uid,
                    from_email=item.from_email,
                    subject=item.subject,
                    received_at=item.received_at,
                    body_text=item.body_text,
                    body_html_present=False,
                    attachments=[],
                    thread_message_id=item.thread_message_id,
                ),
            )
            for item in stored
        ]
        return self._select_next_sender_batch(run_id, messages), highest_seen_uid

    def _select_next_sender_batch(
        self,
        run_id: str,
        messages: list[NormalizedInboundMessage],
    ) -> list[NormalizedInboundMessage]:
        if not messages:
            return []
        first_sender = messages[0].from_email
        selected = [message for message in messages if message.from_email == first_sender]
        deferred = len(messages) - len(selected)
        if deferred > 0:
            LOGGER.info(
                "[%s] Deferring %s unconsumed inbound message(s) from other sender(s).",
                run_id,
                deferred,
            )
        return selected

    def _validate_inbound_batch(self, context: RunContext) -> None:
        if context.mode != "reply":
            return
        senders = {message.from_email for message in context.inbound_messages if message.from_email}
        if len(senders) <= 1:
            return
        context.current_stage = "validation"
        raise ModelValidationError("Reply batch contains multiple senders; refusing to merge messages into one reply.")

    def _load_stage2_for_existing_outbound(self, existing_outbound: StoredOutboundMessage) -> Stage2Result:
        stored_run = self.dependencies.state_store.get_run(existing_outbound.run_id)
        if stored_run is None or not stored_run.stage2_path:
            raise RuntimeError("Recorded outbound message is missing archived stage2 metadata.")
        payload = load_json_file(Path(stored_run.stage2_path), default=None)
        if not isinstance(payload, dict):
            raise RuntimeError("Archived stage2 metadata is missing or malformed.")
        return Stage2Result.from_dict(payload)

    def _is_first_run_bootstrap(self) -> bool:
        return self.dependencies.state_store.get_last_consumed_imap_uid() == 0 and not self.dependencies.state_store.has_outbound_messages()

    def _build_stage1_payload(
        self,
        *,
        mode: str,
        inbound_messages: Sequence[NormalizedInboundMessage],
        now: datetime,
    ) -> dict:
        docs = self.settings.documents
        return {
            "mode": mode,
            "generated_at": now.isoformat(),
            "docs": {
                "memory_promotion_rules": read_required_text(self._memory_promotion_rules_path()),
                "relationship": read_required_text(docs.relationship),
                "user_profile": read_optional_text(docs.user_profile),
                "relationship_memory": read_optional_text(docs.relationship_memory),
                "active_arc": read_optional_text(docs.active_arc),
            },
            "recent_user_episodes": self.dependencies.memory_manager.recent_user_episodes(
                days=RECENT_EPISODE_LOOKBACK_DAYS,
                now=now,
            ),
            "recent_character_episodes": self.dependencies.memory_manager.recent_character_episodes(
                days=RECENT_EPISODE_LOOKBACK_DAYS,
                now=now,
            ),
            "inbound_messages": [item.to_dict() for item in inbound_messages],
        }

    def _memory_promotion_rules_path(self) -> Path:
        default_path = self.settings.documents.memory_promotion_rules
        if self.settings.llm.language != ENGLISH_OUTPUT_LANGUAGE:
            return default_path
        english_path = default_path.with_name("memory_promotion_rules.en.md")
        return english_path if english_path.exists() else default_path

    def _build_stage2_payload(
        self,
        *,
        mode: str,
        inbound_messages: Sequence[NormalizedInboundMessage],
        stage1_result: Stage1Result,
        now: datetime,
    ) -> dict:
        docs = self.settings.documents
        recent_scene_cards = (
            self.dependencies.state_store.list_recent_scene_cards(limit=RECENT_SCENE_CARD_LIMIT)
            if self.settings.llm.image.enabled
            else []
        )
        return {
            "mode": mode,
            "generated_at": now.isoformat(),
            "docs": {
                "persona": read_required_text(docs.persona),
                "world_state": read_required_text(docs.world_state),
                "cast_relations": read_required_text(docs.cast_relations),
                "cast_pool": read_required_text(docs.cast_pool),
                "relationship": read_required_text(docs.relationship),
                "translation_layer": read_required_text(docs.translation_layer),
                "user_profile": read_optional_text(docs.user_profile),
                "relationship_memory": read_optional_text(docs.relationship_memory),
                "active_arc": read_optional_text(docs.active_arc),
            },
            "recent_user_episodes": self.dependencies.memory_manager.recent_user_episodes(
                days=RECENT_EPISODE_LOOKBACK_DAYS,
                now=now,
            ),
            "recent_character_episodes": self.dependencies.memory_manager.recent_character_episodes(
                days=RECENT_EPISODE_LOOKBACK_DAYS,
                now=now,
            ),
            "stage1_result": stage1_result.to_dict(),
            "inbound_messages": [item.to_dict() for item in inbound_messages],
            "recent_scene_cards": recent_scene_cards,
        }

    def _compute_batch_id(
        self,
        *,
        mode: str,
        inbound_messages: Sequence[NormalizedInboundMessage],
        now: datetime,
    ) -> str:
        if mode == "initiative":
            return f"initiative-{now.date().isoformat()}"
        source = "|".join(item.message_id for item in inbound_messages)
        return hash_text(source, prefix="batch-")

    def _validate_stage2(self, stage2_result: Stage2Result) -> None:
        if not stage2_result.subject.strip():
            raise ModelValidationError("Stage 2 subject must not be empty.")
        if not stage2_result.body.strip():
            raise ModelValidationError("Stage 2 body must not be empty.")
        if not self.settings.llm.image.enabled:
            return
        if stage2_result.scene_card is None:
            raise ModelValidationError("Stage 2 scene card is required when image generation is enabled.")
        scene_card = stage2_result.scene_card.to_dict()
        if any(not str(value).strip() for value in scene_card.values()):
            raise ModelValidationError("Scene card requires all fields to be non-empty.")

    def _default_dependencies(self, settings: Settings) -> PipelineDependencies:
        validate_live_settings(settings)
        inbound_client: InboundMailClient = IMAPInboundClient(settings.mail.character_mailbox.imap)
        outbound_client: OutboundMailClient = SMTPOutboundClient(settings.mail.character_mailbox)
        return PipelineDependencies(
            inbound_client=inbound_client,
            outbound_client=outbound_client,
            ai_client=build_ai_client(settings),
            state_store=StateStore(settings),
            memory_manager=MemoryManager(settings),
        )


def build_pipeline(config_path: str = "config.yaml", dependencies: PipelineDependencies | None = None) -> PipelineService:
    settings = load_settings(config_path)
    return PipelineService(settings, dependencies=dependencies)


def _classify_error(exc: Exception, *, current_stage: str) -> str:
    if isinstance(exc, ModelValidationError):
        return "validation"
    if current_stage and current_stage != "pipeline":
        return current_stage
    message = str(exc).lower()
    if "search mailbox" in message or "fetch uid" in message or "imap" in message:
        return "imap"
    if "stage 2" in message:
        return "stage2"
    if "stage 1" in message:
        return "stage1"
    if "image" in message:
        return "image"
    if "smtp" in message or "send" in message:
        return "smtp"
    return "pipeline"


def _is_at_or_before_cutoff(received_at: str, cutoff: datetime | None) -> bool:
    if cutoff is None or not received_at:
        return False
    try:
        parsed = datetime.fromisoformat(received_at)
    except ValueError:
        return False
    if parsed.tzinfo is None and cutoff.tzinfo is not None:
        parsed = parsed.replace(tzinfo=cutoff.tzinfo)
    elif parsed.tzinfo is not None and cutoff.tzinfo is None:
        parsed = parsed.replace(tzinfo=None)
    return parsed <= cutoff
