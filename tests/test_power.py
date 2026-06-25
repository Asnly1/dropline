from __future__ import annotations

import unittest

from dropline.power import parse_power_context


class PowerTests(unittest.TestCase):
    def test_parse_power_context_detects_darkwake_when_clamshell_closed_and_user_inactive(self) -> None:
        context = parse_power_context(
            """
            "AppleClamshellState" = Yes
            "IOPMUserIsActive" = No
            "IOPMUserTriggeredFullWake" = No
            "Wake Type" = "Network"
            """
        )

        self.assertTrue(context.clamshell_closed)
        self.assertFalse(context.user_is_active)
        self.assertFalse(context.user_triggered_full_wake)
        self.assertEqual(context.wake_type, "Network")
        self.assertTrue(context.is_darkwake())

    def test_parse_power_context_keeps_normal_user_wake_out_of_darkwake(self) -> None:
        context = parse_power_context(
            """
            "AppleClamshellState" = No
            "IOPMUserIsActive" = Yes
            "IOPMUserTriggeredFullWake" = No
            "Wake Type" = "UserActivity Assertion"
            """
        )

        self.assertFalse(context.clamshell_closed)
        self.assertTrue(context.user_is_active)
        self.assertFalse(context.is_darkwake())
