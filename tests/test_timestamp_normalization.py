"""Production helpers/write paths, isolated from missing app dependencies."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import load_helpers, IST


class TimestampNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_write_endpoints=True)

    def test_epoch_millis_to_utc_truncates_fractional_second(self):
        actual = self.code["epoch_millis_to_utc"](1783311000900)
        self.assertEqual(actual, datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc))
        self.assertIs(actual.tzinfo, timezone.utc)
        self.assertEqual(actual.microsecond, 0)

    def test_naive_utc_and_aware_ist_normalize_to_same_whole_second(self):
        expected = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
        inputs = [
            datetime(2026, 7, 6, 4, 10, 0, 900000),
            datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST),
        ]
        for value in inputs:
            with self.subTest(value=value):
                actual = self.code["normalize_utc"](value)
                self.assertEqual(actual, expected)
                self.assertIs(actual.tzinfo, timezone.utc)
                self.assertEqual(actual.microsecond, 0)

    def test_epoch_conversion_and_punch_storage_ignore_host_timezone(self):
        expected = datetime(2026, 7, 6, 4, 10, 0, tzinfo=timezone.utc)
        for host_zone in [timezone.utc, IST]:
            with self.subTest(host_zone=host_zone):
                class Clock(datetime):
                    pass

                clock = Clock
                # Simulate fromtimestamp's local-time behaviour when tz is omitted.
                def fromtimestamp(value, tz=None):
                    result = (
                        datetime.fromtimestamp(value, host_zone).replace(tzinfo=None)
                        if tz is None else datetime.fromtimestamp(value, tz)
                    )
                    return Clock.fromisoformat(result.isoformat())

                clock.fromtimestamp = MagicMock(side_effect=fromtimestamp)
                db = MagicMock()
                db.employees.find_one.return_value = {"shift_start": "09:30", "shift_end": "18:30"}
                db.attendance_logs.find_one.return_value = None
                with patch.dict(self.code, datetime=clock, db=db):
                    self.assertEqual(
                        self.code["epoch_millis_to_utc"](1783311000900), expected
                    )
                    self.code["punch_in"](SimpleNamespace(
                        emp_code="EMP0001", punched_at=1783311000900, status="PRESENT"
                    ))
                for call in clock.fromtimestamp.call_args_list:
                    self.assertEqual(call.kwargs.get("tz"), timezone.utc)
                stored = db.attendance_logs.insert_one.call_args.args[0]
                self.assertEqual(stored["punch_in"], expected)
                self.assertIs(stored["punch_in"].tzinfo, timezone.utc)
                self.assertEqual(stored["late_minutes"], 0)

    def test_server_clock_is_aware_utc_and_truncated_for_both_writes(self):
        class Clock(datetime):
            pass

        clock = Clock
        clock.now = MagicMock(return_value=Clock(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc))
        db = MagicMock()
        db.employees.find_one.side_effect = [None, {"shift_start": "09:30", "shift_end": "18:30"}]
        db.attendance_logs.find_one.return_value = None
        employee = MagicMock()
        employee.model_dump.return_value = {"emp_code": "EMP0001"}
        with patch.dict(self.code, datetime=clock, db=db):
            self.code["create_employee"](employee)
            self.code["punch_in"](SimpleNamespace(
                emp_code="EMP0001", punched_at=None, status="PRESENT"
            ))
        self.assertEqual(clock.now.call_count, 2)
        for call in clock.now.call_args_list:
            self.assertEqual(call.args, (timezone.utc,))
        for value in [
            db.employees.insert_one.call_args.args[0]["created_at"],
            db.attendance_logs.insert_one.call_args.args[0]["punch_in"],
        ]:
            self.assertIs(value.tzinfo, timezone.utc)
            self.assertEqual(value.microsecond, 0)
            self.assertEqual(value, datetime(2026, 7, 6, 4, 10, tzinfo=timezone.utc))

    def test_late_boundary_truncates_naive_utc_before_ist_arithmetic(self):
        for second, micros, expected in [(0, 900000, 0), (1, 900000, 10)]:
            for value in [
                datetime(2026, 7, 6, 4, 10, second, micros),
                datetime(2026, 7, 6, 9, 40, second, micros, tzinfo=IST),
            ]:
                with self.subTest(value=value):
                    self.assertEqual(self.code["compute_late_minutes"](
                        value, "09:30"
                    ), expected)

    def test_work_hours_normalizes_mixed_inputs_before_rounding(self):
        # Naive BSON start is UTC; end is aware UTC. Truncation makes the
        # duration 04:00:00 -> 05:07:30: 4050 seconds, or 1.125 hours.
        self.assertEqual(self.code["compute_work_hours"](
            datetime(2026, 7, 6, 4, 0, 0, 900000),
            datetime(2026, 7, 6, 5, 7, 30, tzinfo=timezone.utc),
        ), 1.13)

    def test_overtime_normalizes_naive_utc_and_preserves_minimum(self):
        end_utc = datetime(2026, 7, 6, 13, 0)
        for seconds, expected in [(1799, 0), (1800, 30), (1860, 31)]:
            with self.subTest(seconds=seconds):
                self.assertEqual(self.code["compute_overtime"](
                    end_utc + timedelta(seconds=seconds, microseconds=900000),
                    "18:30", "2026-07-06",
                    shift_start="09:30",
                ), expected)


if __name__ == "__main__":
    unittest.main()
