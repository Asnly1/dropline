from __future__ import annotations

import logging
import time
from datetime import timedelta

from .config import load_settings
from .models import RunOutcome
from .pipeline import PipelineService
from .power import read_power_context
from .scheduler import (
    SCHEDULED_FOR_DATE_KEY,
    SENT_TIME_KEY,
    TARGET_PICKED_AT_KEY,
    TARGET_SEND_TIME_KEY,
    run_scheduler_tick,
)
from .utils import now_in_timezone


LOGGER = logging.getLogger(__name__)
SCHEDULE_STATE_KEYS = (
    SCHEDULED_FOR_DATE_KEY,
    TARGET_PICKED_AT_KEY,
    TARGET_SEND_TIME_KEY,
    SENT_TIME_KEY,
)


def _read_schedule_state(pipeline: PipelineService) -> dict[str, str]:
    store = pipeline.dependencies.state_store
    return {key: (store.get_kv(key) or "") for key in SCHEDULE_STATE_KEYS}


def _format_schedule_state(state: dict[str, str]) -> str:
    return (
        f"scheduled_for_date={state[SCHEDULED_FOR_DATE_KEY] or '-'} "
        f"target_picked_at={state[TARGET_PICKED_AT_KEY] or '-'} "
        f"target_send_time={state[TARGET_SEND_TIME_KEY] or '-'} "
        f"sent_time={state[SENT_TIME_KEY] or '-'}"
    )


def _classify_tick_event(
    *,
    started_at,
    before_state: dict[str, str],
    after_state: dict[str, str],
    outcome: RunOutcome | None,
) -> str:
    if outcome is not None:
        yesterday = (started_at.date() - timedelta(days=1)).isoformat()
        if (
            before_state[SCHEDULED_FOR_DATE_KEY] == yesterday
            and before_state[TARGET_SEND_TIME_KEY]
            and not before_state[SENT_TIME_KEY]
        ):
            return "catch_up_yesterday"
        return "run_succeeded" if outcome.success else "run_failed"

    if (
        not before_state[TARGET_SEND_TIME_KEY]
        and after_state[SCHEDULED_FOR_DATE_KEY] == started_at.date().isoformat()
        and after_state[TARGET_SEND_TIME_KEY]
    ):
        return "picked_today_target"
    return "skipped"


def scheduler_tick_command(config_path: str = "config.yaml") -> int:
    settings = load_settings(config_path)
    logging.basicConfig(
        level=getattr(logging, settings.logging.level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    started_at = now_in_timezone(settings.project.timezone)
    started_perf = time.perf_counter()
    tick_id = started_at.strftime("tick-%Y%m%dT%H%M%S%z")
    LOGGER.info("tick_started tick_id=%s started_at=%s", tick_id, started_at.isoformat())

    power_context = read_power_context()
    if power_context is not None and power_context.is_darkwake():
        finished_at = now_in_timezone(settings.project.timezone)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        unloaded_state = {key: "" for key in SCHEDULE_STATE_KEYS}
        LOGGER.info(
            "tick_finished tick_id=%s finished_at=%s duration_ms=%s event=skipped_darkwake power_state=%s state_after=%s",
            tick_id,
            finished_at.isoformat(),
            duration_ms,
            power_context.to_log_string(),
            _format_schedule_state(unloaded_state),
        )
        return 0

    try:
        pipeline = PipelineService(settings)
    except ValueError as exc:
        finished_at = now_in_timezone(settings.project.timezone)
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        LOGGER.error(
            "tick_finished tick_id=%s finished_at=%s duration_ms=%s event=configuration_error message=%s",
            tick_id,
            finished_at.isoformat(),
            duration_ms,
            exc,
        )
        return 1

    pipeline.dependencies.state_store.initialize()
    before_state = _read_schedule_state(pipeline)
    LOGGER.info("tick_state tick_id=%s state_before=%s", tick_id, _format_schedule_state(before_state))

    outcome = run_scheduler_tick(settings, pipeline)
    after_state = _read_schedule_state(pipeline)
    finished_at = now_in_timezone(settings.project.timezone)
    duration_ms = int((time.perf_counter() - started_perf) * 1000)
    event = _classify_tick_event(
        started_at=started_at,
        before_state=before_state,
        after_state=after_state,
        outcome=outcome,
    )
    if outcome is None:
        LOGGER.info(
            "tick_finished tick_id=%s finished_at=%s duration_ms=%s event=%s state_after=%s",
            tick_id,
            finished_at.isoformat(),
            duration_ms,
            event,
            _format_schedule_state(after_state),
        )
        return 0
    if outcome.success:
        LOGGER.info(
            "tick_finished tick_id=%s finished_at=%s duration_ms=%s event=%s run_id=%s mode=%s batch_id=%s state_after=%s",
            tick_id,
            finished_at.isoformat(),
            duration_ms,
            event,
            outcome.run_id,
            outcome.mode,
            outcome.batch_id,
            _format_schedule_state(after_state),
        )
        return 0
    LOGGER.error(
        "tick_finished tick_id=%s finished_at=%s duration_ms=%s event=%s run_id=%s error_stage=%s message=%s state_after=%s",
        tick_id,
        finished_at.isoformat(),
        duration_ms,
        event,
        outcome.run_id,
        outcome.error_stage,
        outcome.message,
        _format_schedule_state(after_state),
    )
    return 1
