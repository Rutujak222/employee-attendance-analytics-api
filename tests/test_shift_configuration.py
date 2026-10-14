"""Production configuration and validation checks without HTTP dependencies."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfoNotFoundError

from test_calculation_helpers import load_helpers


class HTTPExceptionStub(Exception):
    """Capture exception arguments; this does not verify FastAPI HTTP encoding."""
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class ShiftConfigurationTests(unittest.TestCase):
    def test_missing_zone_data_warns_once_and_preserves_ist(self):
        with patch("zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing")), \
                patch("logging.Logger.warning") as warning:
            code = load_helpers()
            self.assertEqual(code["IST"].utcoffset(None), timedelta(hours=5, minutes=30))
            self.assertEqual(code["IST"].tzname(None), "Asia/Kolkata")
            for _ in range(2):
                self.assertEqual(code["attendance_date"](
                    datetime(2026, 7, 6, 18, 30, tzinfo=timezone.utc), "09:30", "18:30"
                ), "2026-07-07")
            warning.assert_called_once_with(
                "Asia/Kolkata timezone data unavailable; using fixed UTC+05:30."
            )

    def test_available_zone_path_uses_provider_without_warning(self):
        # Simulate successful zone loading; actual IANA data is absent here.
        supplied_zone = timezone(timedelta(hours=5, minutes=30), name="Asia/Kolkata")
        with patch("zoneinfo.ZoneInfo", return_value=supplied_zone) as provider, \
                patch("logging.Logger.warning") as warning:
            code = load_helpers()
        provider.assert_called_once_with("Asia/Kolkata")
        self.assertIs(code["IST"], supplied_zone)
        warning.assert_not_called()

    def test_equal_shift_times_rejected_before_database_access(self):
        code = load_helpers(include_write_endpoints=True)
        db = MagicMock()
        with patch.dict(code, db=db, HTTPException=HTTPExceptionStub):
            with self.assertRaises(HTTPExceptionStub) as raised:
                code["create_employee"](SimpleNamespace(shift_start="06:00", shift_end="06:00"))
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, [{
            "type": "value_error", "loc": ["body", "shift_end"],
            "msg": "Value error, shift_start must differ from shift_end",
        }])
        self.assertEqual(db.mock_calls, [])

    def test_unequal_day_and_overnight_shifts_can_be_created(self):
        code = load_helpers(include_write_endpoints=True)
        for start, end in [("09:30", "18:30"), ("22:00", "06:00")]:
            with self.subTest(start=start, end=end):
                db = MagicMock()
                db.employees.find_one.return_value = None
                fields = {"emp_code": "EMP0001", "shift_start": start, "shift_end": end}
                body = SimpleNamespace(**fields, model_dump=lambda: dict(fields))
                with patch.dict(code, db=db):
                    response = code["create_employee"](body)
                db.employees.insert_one.assert_called_once()
                self.assertEqual((response["shift_start"], response["shift_end"]), (start, end))

    def test_missing_overtime_context_cannot_produce_a_result(self):
        code = load_helpers()
        punch_out = datetime(2026, 7, 7, 1, 10, tzinfo=timezone.utc)
        with self.assertRaisesRegex(TypeError, "shift_start"):
            code["compute_overtime"](punch_out, "06:00", "2026-07-06")
        with self.assertRaisesRegex(ValueError, "shift_start is required"):
            code["compute_overtime"](punch_out, "06:00", "2026-07-06", shift_start=None)
        self.assertEqual(code["compute_overtime"](
            punch_out, "06:00", "2026-07-06", shift_start="22:00"
        ), 40)


if __name__ == "__main__":
    unittest.main()
