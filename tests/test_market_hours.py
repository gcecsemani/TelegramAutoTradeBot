"""The signal bot runs on weekday sessions only."""

import os
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytz                                              # noqa: E402

from core.market_hours import should_run                 # noqa: E402

IST = pytz.timezone("Asia/Kolkata")


def at(year, month, day, hour, minute):
    return IST.localize(datetime(year, month, day, hour, minute))


class MarketHoursTests(unittest.TestCase):
    def test_weekdays_run_from_before_the_open_until_after_mcx(self):
        self.assertFalse(should_run(at(2026, 11, 2, 8, 44)))    # Monday
        self.assertTrue(should_run(at(2026, 11, 2, 8, 45)))
        self.assertTrue(should_run(at(2026, 11, 2, 15, 40)))
        self.assertTrue(should_run(at(2026, 11, 2, 23, 50)))
        self.assertTrue(should_run(at(2026, 11, 3, 0, 9)))      # still Monday's session
        self.assertFalse(should_run(at(2026, 11, 3, 0, 10)))

    def test_weekends_stay_stopped(self):
        self.assertTrue(should_run(at(2026, 11, 7, 0, 5)))      # Saturday, Friday's tail
        self.assertFalse(should_run(at(2026, 11, 7, 10, 0)))
        self.assertFalse(should_run(at(2026, 11, 8, 10, 0)))    # Sunday
        self.assertFalse(should_run(at(2026, 11, 9, 0, 5)))     # Monday before the open

    def test_a_holiday_stays_stopped_including_the_next_morning(self):
        closed = {"2026-11-02"}
        self.assertFalse(should_run(at(2026, 11, 2, 10, 0), closed))
        self.assertFalse(should_run(at(2026, 11, 3, 0, 5), closed))


if __name__ == "__main__":
    unittest.main()
