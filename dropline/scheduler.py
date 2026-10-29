from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime, timedelta
from datetime import time as dt_time
from zoneinfo import ZoneInfo

from .config import Settings
from .models import RunOutcome
from .pipeline import PipelineService
from .utils import now_in_timezone

LOGGER = logging.getLogger(__name__)
SCHEDULED_FOR_DATE_KEY = "scheduled_for_date"
TARGET_PICKED_AT_KEY = "target_picked_at"
TARGET_SEND_TIME_KEY = "target_send_time"
SENT_TIME_KEY = "sent_time"


@dataclass(frozen=True, slots=True)
class ScheduleState:
    scheduled_for_date: dt_date
    target_picked_at: datetime | None
    target_send_time: datetime | None
    sent_time: datetime | None


def parse_hhmm(value: str) -> dt_time:
    hour_str, minute_str = value.split(":")
    return dt_time(hour=int(hour_str), minute=int(minute_str))


def _parse_timestamp(value: str, now: datetime) -> datetime | None:
    raw_value = str(value).strip()
    if not raw_value:
        return None
    try:
        parsed = datetime.fromisoformat(raw_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=now.tzinfo)
    return parsed


def _parse_schedule_date(value: str) -> dt_date | None:
    raw_value = str(value).strip()
    if not raw_value:
        return None
    try:
        return dt_date.fromisoformat(raw_value)
    except ValueError:
        return None


def _clear_schedule_state(pipeline: PipelineService) -> None:
    store = pipeline.dependencies.state_store
    for key in (SCHEDULED_FOR_DATE_KEY, TARGET_PICKED_AT_KEY, TARGET_SEND_TIME_KEY, SENT_TIME_KEY):
        store.set_kv(key, "")


def _load_schedule_state(pipeline: PipelineService, now: datetime) -> ScheduleState | None:
    store = pipeline.dependencies.state_store
    scheduled_for_date = _parse_schedule_date(store.get_kv(SCHEDULED_FOR_DATE_KEY) or "")
    if scheduled_for_date is None:
        return None
    if scheduled_for_date < now.date() - timedelta(days=1) or scheduled_for_date > now.date():
        _clear_schedule_state(pipeline)
        return None
    return ScheduleState(
        scheduled_for_date=scheduled_for_date,
        target_picked_at=_parse_timestamp(store.get_kv(TARGET_PICKED_AT_KEY) or "", now),
        target_send_time=_parse_timestamp(store.get_kv(TARGET_SEND_TIME_KEY) or "", now),
        sent_time=_parse_timestamp(store.get_kv(SENT_TIME_KEY) or "", now),
    )


def _save_schedule_state(pipeline: PipelineService, state: ScheduleState) -> None:
    store = pipeline.dependencies.state_store
    store.set_kv(SCHEDULED_FOR_DATE_KEY, state.scheduled_for_date.isoformat())
    store.set_kv(TARGET_PICKED_AT_KEY, state.target_picked_at.isoformat() if state.target_picked_at else "")
    store.set_kv(TARGET_SEND_TIME_KEY, state.target_send_time.isoformat() if state.target_send_time else "")
    store.set_kv(SENT_TIME_KEY, state.sent_time.isoformat() if state.sent_time else "")


def _pick_allowed_at(settings: Settings, scheduled_for_date: dt_date) -> datetime:
    return datetime.combine(
        scheduled_for_date,
        parse_hhmm(settings.schedule.daily_target_pick_time),
        tzinfo=ZoneInfo(settings.project.timezone),
    )


def _window_crosses_midnight(settings: Settings) -> bool:
    return parse_hhmm(settings.schedule.window_end) < parse_hhmm(settings.schedule.window_start)


def _window_bounds(settings: Settings, scheduled_for_date: dt_date, tzinfo) -> tuple[datetime, datetime]:
    start = datetime.combine(scheduled_for_date, parse_hhmm(settings.schedule.window_start), tzinfo=tzinfo)
    end = datetime.combine(scheduled_for_date, parse_hhmm(settings.schedule.window_end), tzinfo=tzinfo)
    if end < start:
        end += timedelta(days=1)
    return start, end


def _window_end(settings: Settings, scheduled_for_date: dt_date, tzinfo) -> datetime:
    return _window_bounds(settings, scheduled_for_date, tzinfo)[1]


def _pick_schedule_state_for_today(settings: Settings, pipeline: PipelineService, now: datetime) -> ScheduleState | None:
    if now < _pick_allowed_at(settings, now.date()):
        return None
    state = ScheduleState(
        scheduled_for_date=now.date(),
        target_picked_at=now,
        target_send_time=pick_target_send_time(settings, now),
        sent_time=None,
    )
    _save_schedule_state(pipeline, state)
    LOGGER.info("Scheduled tick picked today's target send time: %s", state.target_send_time.isoformat())
    return state


def _mark_schedule_sent(pipeline: PipelineService, state: ScheduleState, sent_at: datetime) -> None:
    _save_schedule_state(
        pipeline,
        ScheduleState(
            scheduled_for_date=state.scheduled_for_date,
            target_picked_at=state.target_picked_at,
            target_send_time=state.target_send_time,
            sent_time=sent_at,
        ),
    )


def pick_target_send_time(settings: Settings, now: datetime) -> datetime:
    start, end = _window_bounds(settings, now.date(), ZoneInfo(settings.project.timezone))
    total_minutes = int((end - start).total_seconds() // 60)
    seed = f"{settings.project.name}:{now.date().isoformat()}"
    rng = random.Random(seed)
    return start + timedelta(minutes=rng.randint(0, max(total_minutes, 0)))


def run_scheduler_tick(
    settings: Settings,
    pipeline: PipelineService,
    *,
    now: datetime | None = None,
) -> RunOutcome | None:
    pipeline.dependencies.state_store.initialize()
    current_now = now or now_in_timezone(settings.project.timezone)
    if not settings.schedule.enabled:
        LOGGER.info("Scheduled tick skipped: schedule disabled.")
        return None

    state = _load_schedule_state(pipeline, current_now)
    yesterday = current_now.date() - timedelta(days=1)

    if state and state.scheduled_for_date < current_now.date():
        state_window_end = _window_end(settings, state.scheduled_for_date, current_now.tzinfo)
        if current_now <= state_window_end:
            LOGGER.info("Scheduled tick is still inside previous day's cross-midnight window.")
        elif (
            state.scheduled_for_date == yesterday
            and not _window_crosses_midnight(settings)
            and state.sent_time is None
            and state.target_send_time is not None
        ):
            LOGGER.info(
                "Scheduled tick detected yesterday's missed send for %s; catching up immediately.",
                yesterday.isoformat(),
            )
            outcome = pipeline.run_once()
            if outcome.success:
                _mark_schedule_sent(pipeline, state, current_now)
                LOGGER.info("Scheduled catch-up succeeded: %s", outcome.run_id)
            else:
                LOGGER.error("Scheduled catch-up failed at %s: %s", outcome.error_stage, outcome.message)
            return outcome
        else:
            _clear_schedule_state(pipeline)
            state = None

    if state is None:
        state = _pick_schedule_state_for_today(settings, pipeline, current_now)
        if state is None:
            LOGGER.info("Scheduled tick skipped: waiting for daily_target_pick_time.")
            return None

    if state.sent_time is not None:
        LOGGER.info("Scheduled tick skipped: schedule for %s already sent.", state.scheduled_for_date.isoformat())
        return None

    if state.target_send_time is None:
        LOGGER.info("Scheduled tick skipped: schedule for %s has no target send time.", state.scheduled_for_date.isoformat())
        return None

    window_end = _window_end(settings, state.scheduled_for_date, current_now.tzinfo)
    if not (state.target_send_time <= current_now <= window_end):
        LOGGER.info("Scheduled tick skipped: not due yet.")
        return None

    outcome = pipeline.run_once()
    if outcome.success:
        _mark_schedule_sent(pipeline, state, current_now)
        LOGGER.info("Scheduled run succeeded: %s", outcome.run_id)
    else:
        LOGGER.error("Scheduled run failed at %s: %s", outcome.error_stage, outcome.message)
    return outcome
