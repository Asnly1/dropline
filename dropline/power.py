from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)
IOREG_VALUE_RE = re.compile(r'"([^"]+)"\s*=\s*(?:"([^"]*)"|(Yes|No))')


@dataclass(frozen=True, slots=True)
class PowerContext:
    clamshell_closed: bool | None
    user_is_active: bool | None
    user_triggered_full_wake: bool | None
    wake_type: str

    def is_darkwake(self) -> bool:
        if self.clamshell_closed is True and self.user_is_active is False:
            return True
        return bool(
            self.user_is_active is False
            and self.user_triggered_full_wake is False
            and self.wake_type
            and self.wake_type != "UserActivity Assertion"
        )

    def to_log_string(self) -> str:
        return (
            f"clamshell_closed={_format_bool(self.clamshell_closed)} "
            f"user_is_active={_format_bool(self.user_is_active)} "
            f"user_triggered_full_wake={_format_bool(self.user_triggered_full_wake)} "
            f"wake_type={self.wake_type or '-'}"
        )


def read_power_context() -> PowerContext | None:
    try:
        result = subprocess.run(
            ["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        LOGGER.warning("ioreg is not available; skipping DarkWake detection.")
        return None
    if result.returncode != 0:
        LOGGER.warning("ioreg returned %s while reading power context.", result.returncode)
        return None
    return parse_power_context(result.stdout)


def parse_power_context(raw_output: str) -> PowerContext:
    values: dict[str, str] = {}
    for line in raw_output.splitlines():
        match = IOREG_VALUE_RE.search(line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2) or match.group(3) or ""
        values[key] = value
    return PowerContext(
        clamshell_closed=_parse_yes_no(values.get("AppleClamshellState")),
        user_is_active=_parse_yes_no(values.get("IOPMUserIsActive")),
        user_triggered_full_wake=_parse_yes_no(values.get("IOPMUserTriggeredFullWake")),
        wake_type=values.get("Wake Type", ""),
    )


def _parse_yes_no(value: str | None) -> bool | None:
    if value == "Yes":
        return True
    if value == "No":
        return False
    return None


def _format_bool(value: bool | None) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "-"
