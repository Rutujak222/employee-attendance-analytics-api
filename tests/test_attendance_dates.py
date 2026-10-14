"""R1 shift boundaries and attendance response identity, using production code."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import IST, load_helpers


class AttendanceDateTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_write_endpoints=True, include_list_endpoints=True)

    def test_normal_shift_uses_kolkata_midnight_not_utc_midnight(self):
        self.assertEqual(
            getattr(self.code["IST"], "key", None) or self.code["IST"].tzname(None),
            "Asia/Kolkata",
        )
        cases = [
            (datetime(2026, 7, 6, 18, 29, 59, 999000, tzinfo=timezone.utc), "2026-07-06"),
            (datetime(2026, 7, 6, 18, 30, tzinfo=timezone.utc), "2026-07-07"),
            (datetime(2026, 7, 6, 23, 59, 59, tzinfo=timezone.utc), "2026-07-07"),
            (datetime(2026, 7, 7, 0, 0, tzinfo=timezone.utc), "2026-07-07"),
        ]
        for instant, expected in cases:
            with self.subTest(instant=instant):
                self.assertEqual(self.code["attendance_date"](
                    instant, "09:30", "18:30"
                ), expected)

    def test_overnight_date_and_lateness_at_end_boundary(self):
        cases = [
            (datetime(2026, 7, 6, 21, 55, tzinfo=IST), "2026-07-06", 0),
            (datetime(2026, 7, 6, 22, 10, tzinfo=IST), "2026-07-06", 0),
            (datetime(2026, 7, 6, 22, 10, 1, tzinfo=IST), "2026-07-06", 10),
            (datetime(2026, 7, 7, 2, 0, tzinfo=IST), "2026-07-06", 240),
            (datetime(2026, 7, 7, 5, 59, 59, tzinfo=IST), "2026-07-06", 479),
            (datetime(2026, 7, 7, 6, 0, 0, 900000, tzinfo=IST), "2026-07-07", 0),
            (datetime(2026, 7, 7, 6, 0, 1, tzinfo=IST), "2026-07-07", 0),
        ]
        for instant, expected_date, expected_late in cases:
            with self.subTest(instant=instant):
                self.assertEqual(self.code["attendance_date"](
                    instant, "22:00", "06:00"
                ), expected_date)
                self.assertEqual(self.code["compute_late_minutes"](
                    instant, "22:00", shift_end="06:00"
                ), expected_late)

    def test_punch_in_never_uses_host_timezone_for_calendar_date(self):
        class ExplicitZoneDatetime(datetime):
            def astimezone(self, tz=None):
                if tz is None:
                    raise AssertionError("Host-local timezone must not determine attendance date")
                return super().astimezone(tz)

        instant = ExplicitZoneDatetime(2026, 7, 6, 18, 30, tzinfo=timezone.utc)
        db = MagicMock()
        db.employees.find_one.return_value = {"shift_start": "09:30", "shift_end": "18:30"}
        db.attendance_logs.find_one.return_value = None
        with patch.dict(self.code, db=db, epoch_millis_to_utc=lambda value: instant):
            result = self.code["punch_in"](SimpleNamespace(
                emp_code="EMP0001", punched_at=1783362600000, status="PRESENT"
            ))
        self.assertEqual(result["date"], "2026-07-07")
        self.assertEqual(result["late_minutes"], 0)

    def test_equal_shift_times_follow_overnight_rule(self):
        instant = datetime(2026, 7, 7, 5, 0, tzinfo=IST)
        self.assertEqual(self.code["attendance_date"](instant, "06:00", "06:00"), "2026-07-06")
        self.assertEqual(self.code["compute_late_minutes"](
            instant, "06:00", shift_end="06:00"
        ), 1380)
        self.assertEqual(self.code["attendance_date"](
            datetime(2026, 7, 7, 6, 0, tzinfo=IST), "06:00", "06:00"
        ), "2026-07-07")

    def test_overnight_overtime_anchors_end_to_next_day(self):
        scheduled_end = datetime(2026, 7, 7, 6, 0, tzinfo=IST)
        for start in ["22:00", "06:00"]:
            for seconds, expected in [(0, 0), (1799, 0), (1800, 30), (2400, 40)]:
                with self.subTest(shift_start=start, seconds=seconds):
                    self.assertEqual(self.code["compute_overtime"](
                        scheduled_end + timedelta(seconds=seconds), "06:00",
                        "2026-07-06", shift_start=start,
                    ), expected)
        self.assertEqual(self.code["compute_overtime"](
            datetime(2026, 7, 6, 19, 10, tzinfo=IST), "18:30",
            "2026-07-06", shift_start="09:30",
        ), 40)

    def test_overnight_work_hours_use_complete_punch_out(self):
        self.assertEqual(self.code["compute_work_hours"](
            datetime(2026, 7, 6, 21, 55, tzinfo=IST),
            datetime(2026, 7, 7, 6, 40, tzinfo=IST),
        ), 8.75)

    def test_punch_in_and_list_use_only_natural_response_identity(self):
        db = MagicMock()
        db.employees.find_one.return_value = {"shift_start": "22:00", "shift_end": "06:00"}
        db.attendance_logs.find_one.return_value = None

        def insert(document):
            document["_id"] = "mongo-internal"
            stored.append(deepcopy(document))
            return SimpleNamespace(inserted_id="mongo-internal")

        stored = []
        db.attendance_logs.insert_one.side_effect = insert
        with patch.dict(self.code, db=db):
            punched = self.code["punch_in"](SimpleNamespace(
                emp_code="EMP0001", punched_at=1783369800000, status="PRESENT"
            ))
            db.attendance_logs.find.return_value.sort.return_value.skip.return_value.limit.return_value = deepcopy(stored)
            listed = self.code["list_attendance"]()["items"][0]
        self.assertEqual(punched, listed)
        self.assertEqual(set(punched), {
            "emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
            "late_minutes", "overtime_minutes", "half_day", "history",
        })
        self.assertEqual((punched["emp_code"], punched["date"]), ("EMP0001", "2026-07-06"))
        self.assertEqual(punched["late_minutes"], 240)
        self.assertEqual(punched["punch_in"], 1783369800000)
        self.assertEqual(stored[0]["_id"], "mongo-internal")
        self.assertIsInstance(stored[0]["punch_in"], datetime)
        self.assertNotIn("id", stored[0])


if __name__ == "__main__":
    unittest.main()
