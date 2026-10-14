"""Actual punch-out/correction functions with MongoDB operations isolated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import IST, load_helpers
from test_shift_configuration import HTTPExceptionStub


def epoch(value):
    return int(value.timestamp() * 1000)


def correction(**changes):
    values = {"reason": "biometric correction", "regularized_by": "hr.admin", **changes}
    return SimpleNamespace(**values, model_dump=lambda **kwargs: dict(values))


class AttendanceWriteTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_write_endpoints=True)
        self.db = MagicMock()
        self.employee = {"shift_start": "09:30", "shift_end": "18:30"}
        self.db.employees.find_one.return_value = self.employee
        self.record = {
            "_id": "internal", "emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT",
            "punch_in": datetime(2026, 7, 6, 4, 0), "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": [],
        }
        self.db.attendance_logs.find_one.return_value = self.record
        self.db.attendance_logs.update_one.return_value.matched_count = 1
        self.patch = patch.dict(self.code, db=self.db, HTTPException=HTTPExceptionStub)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def close(self, when):
        return self.code["punch_out"](SimpleNamespace(emp_code="EMP0001", punched_at=epoch(when)))

    def edit(self, body=None, date="2026-07-06"):
        return self.code["regularize_attendance"]("EMP0001", date, body or correction(status="WFH"))

    def assert_error(self, status, action):
        with self.assertRaises(HTTPExceptionStub) as raised:
            action()
        self.assertEqual(raised.exception.status_code, status)
        return raised.exception

    def test_day_punch_out_selects_latest_eligible_record_and_never_writes_history(self):
        when = datetime(2026, 7, 6, 19, 10, 0, 900000, tzinfo=IST)
        before = deepcopy(self.record)
        response = self.close(when)
        normalized = when.astimezone(timezone.utc).replace(microsecond=0)
        self.db.attendance_logs.find_one.assert_called_once_with(
            {"emp_code": "EMP0001", "punch_in": {"$type": "date", "$lte": normalized}},
            sort=[("punch_in", -1)])
        self.assertEqual((response["work_hours"], response["overtime_minutes"], response["half_day"]), (9.67, 40, False))
        self.assertEqual(response["history"], [])
        self.assertEqual(response["punch_out"], epoch(normalized))
        self.assertNotIn("_id", response)
        self.assertNotIn("id", response)
        query, update = self.db.attendance_logs.update_one.call_args.args
        self.assertEqual(query["punch_out"], {"$eq": None})
        self.assertEqual(query["punch_in"], {"$eq": before["punch_in"]})
        self.assertEqual(query["history"], {"$eq": []})
        self.assertEqual(set(update), {"$set"})
        self.assertNotIn("history", update["$set"])
        self.assertEqual(update["$set"]["punch_out"], normalized)
        self.assertEqual(self.record, before)

    def test_overnight_punch_out_uses_record_date_and_complete_timestamp(self):
        self.employee.update(shift_start="22:00", shift_end="06:00")
        self.record["punch_in"] = datetime(2026, 7, 6, 16, 25)
        response = self.close(datetime(2026, 7, 7, 6, 40, tzinfo=IST))
        self.assertEqual(response["date"], "2026-07-06")
        self.assertEqual((response["work_hours"], response["overtime_minutes"]), (8.75, 40))

    def test_punch_out_half_day_uses_rounded_hours(self):
        start = self.record["punch_in"].replace(tzinfo=timezone.utc)
        for seconds, hours, half in [(16181, 4.49, True), (16182, 4.50, False), (4050, 1.13, True)]:
            with self.subTest(seconds=seconds):
                result = self.close(start + timedelta(seconds=seconds))
                self.assertEqual((result["work_hours"], result["half_day"]), (hours, half))

    def test_punch_out_interval_equal_and_24_hour_boundaries(self):
        start = self.record["punch_in"].replace(tzinfo=timezone.utc)
        for seconds in (0, 86401):
            self.assert_error(422, lambda: self.close(start + timedelta(seconds=seconds)))
        self.db.attendance_logs.update_one.assert_not_called()
        self.assertEqual(self.close(start + timedelta(hours=24))["work_hours"], 24.0)

    def test_punch_out_missing_employee_record_and_already_closed(self):
        when = datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)
        self.db.employees.find_one.return_value = None
        self.assert_error(404, lambda: self.close(when))
        self.db.attendance_logs.find_one.assert_not_called()
        self.db.employees.find_one.return_value = self.employee
        self.db.attendance_logs.find_one.return_value = None
        self.assert_error(404, lambda: self.close(when))
        self.db.attendance_logs.find_one.return_value = self.record
        self.record["punch_out"] = datetime(2026, 7, 6, 12, 0)
        self.assert_error(409, lambda: self.close(when))
        self.db.attendance_logs.update_one.assert_not_called()

    def test_punch_out_omission_uses_whole_second_utc_clock(self):
        class Clock(datetime):
            @classmethod
            def now(cls, tz):
                self.assertIs(tz, timezone.utc)
                return cls(2026, 7, 6, 13, 0, 0, 900000, tzinfo=tz)
        self.record["punch_in"] = Clock(2026, 7, 6, 4, 0)
        with patch.dict(self.code, datetime=Clock):
            response = self.code["punch_out"](SimpleNamespace(emp_code="EMP0001", punched_at=None))
        self.assertEqual(response["punch_out"], epoch(datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)))
        stored = self.db.attendance_logs.update_one.call_args.args[1]["$set"]["punch_out"]
        self.assertIs(stored.tzinfo, timezone.utc)
        self.assertEqual(stored.microsecond, 0)

    def test_regularization_recomputes_and_appends_exact_audit_changes(self):
        old_entry = {"at": datetime(2026, 7, 5, 4, 0), "by": "previous", "reason": "old change", "changes": {}}
        self.record.update(punch_in=datetime(2026, 7, 6, 4, 35), punch_out=datetime(2026, 7, 6, 13, 40),
                           work_hours=9.08, late_minutes=35, overtime_minutes=40, history=[old_entry])
        before = deepcopy(self.record)
        when = datetime(2026, 7, 6, 9, 28, 0, 900000, tzinfo=IST)
        result = self.edit(correction(punch_in=epoch(when)))
        self.assertEqual((result["work_hours"], result["late_minutes"], result["overtime_minutes"]), (9.7, 0, 40))
        self.assertEqual(len(result["history"]), 2)
        query, update = self.db.attendance_logs.update_one.call_args.args
        self.assertEqual(query["history"], {"$eq": [old_entry]})
        entry = update["$push"]["history"]
        self.assertEqual(set(entry), {"at", "by", "reason", "changes"})
        self.assertEqual(set(entry["changes"]), {"punch_in", "late_minutes", "work_hours"})
        self.assertEqual(entry["changes"]["late_minutes"], {"from": 35, "to": 0})
        self.assertIsInstance(entry["changes"]["punch_in"]["from"], datetime)
        self.assertIsInstance(entry["changes"]["punch_in"]["to"], datetime)
        self.assertIs(entry["at"].tzinfo, timezone.utc)
        self.assertEqual(entry["at"].microsecond, 0)
        self.assertEqual((entry["by"], entry["reason"]), ("hr.admin", "biometric correction"))
        self.assertNotIn("history", update["$set"])
        self.assertIsInstance(result["history"][-1]["changes"]["punch_in"]["to"], int)
        self.assertEqual(self.record, before)

    def test_regularization_absent_and_leave_clear_times_and_derived_fields(self):
        self.record.update(punch_out=datetime(2026, 7, 6, 13, 0), work_hours=9.0,
                           late_minutes=10, overtime_minutes=30, half_day=True)
        for status in ("ABSENT", "LEAVE"):
            with self.subTest(status=status):
                result = self.edit(correction(status=status))
                self.assertEqual(result["status"], status)
                for field in ("punch_in", "punch_out", "work_hours"):
                    self.assertIsNone(result[field])
                self.assertEqual((result["late_minutes"], result["overtime_minutes"], result["half_day"]), (0, 0, False))

    def test_absent_with_supplied_times_rejected_before_database_access(self):
        for status in ("ABSENT", "LEAVE"):
            for field in ("punch_in", "punch_out"):
                self.assert_error(422, lambda: self.edit(correction(status=status, **{field: 1783311000000})))
        self.assertEqual(self.db.mock_calls, [])

    def test_presence_requires_punch_in_and_absent_can_be_regularized_to_presence(self):
        self.record.update(status="ABSENT", punch_in=None)
        self.assert_error(422, lambda: self.edit(correction(status="WFH")))
        self.db.attendance_logs.update_one.assert_not_called()
        result = self.edit(correction(status="WFH", punch_in=1783311000900))
        self.assertEqual(result["status"], "WFH")
        self.assertEqual(result["punch_in"], 1783311000000)
        self.assertIsNone(result["punch_out"])
        self.assertIsNone(result["work_hours"])
        self.assertEqual((result["late_minutes"], result["overtime_minutes"], result["half_day"]), (0, 0, False))

    def test_regularization_rejects_wrong_attendance_day_and_bad_intervals(self):
        next_day = epoch(datetime(2026, 7, 7, 9, 30, tzinfo=IST))
        self.assert_error(422, lambda: self.edit(correction(punch_in=next_day)))
        start = self.record["punch_in"].replace(tzinfo=timezone.utc)
        for seconds in (-1, 0, 86401):
            self.assert_error(422, lambda: self.edit(correction(punch_out=epoch(start + timedelta(seconds=seconds)))))
        self.db.attendance_logs.update_one.assert_not_called()
        result = self.edit(correction(punch_out=epoch(start + timedelta(hours=24))))
        self.assertEqual(result["work_hours"], 24.0)

    def test_regularization_overnight_lateness_overtime_and_half_day(self):
        self.employee.update(shift_start="22:00", shift_end="06:00")
        self.record["punch_in"] = datetime(2026, 7, 6, 16, 30)
        result = self.edit(correction(
            punch_in=epoch(datetime(2026, 7, 7, 2, 0, tzinfo=IST)),
            punch_out=epoch(datetime(2026, 7, 7, 6, 40, tzinfo=IST))))
        self.assertEqual((result["date"], result["late_minutes"], result["overtime_minutes"], result["work_hours"]),
                         ("2026-07-06", 240, 40, 4.67))
        self.assertFalse(result["half_day"])
        self.assert_error(422, lambda: self.edit(correction(
            punch_in=epoch(datetime(2026, 7, 7, 6, 0, tzinfo=IST)))))

    def test_regularization_noop_including_same_truncated_time_is_422(self):
        when = epoch(self.record["punch_in"].replace(tzinfo=timezone.utc)) + 900
        for body in (correction(), correction(status="PRESENT"), correction(punch_in=when),
                     correction(work_hours=99)):
            self.assert_error(422, lambda: self.edit(body))
        self.db.attendance_logs.update_one.assert_not_called()

    def test_regularization_missing_records_and_invalid_path_date(self):
        for value in ("", "2026-02-30", "2026-7-06", "20260706"):
            self.assert_error(422, lambda: self.edit(date=value))
        self.assertEqual(self.db.mock_calls, [])
        self.db.employees.find_one.return_value = None
        self.assert_error(404, self.edit)
        self.db.attendance_logs.find_one.assert_not_called()
        self.db.employees.find_one.return_value = self.employee
        self.db.attendance_logs.find_one.return_value = None
        self.assert_error(404, self.edit)
        self.db.attendance_logs.update_one.assert_not_called()

    def test_legacy_history_is_appended_and_missing_fields_are_guarded(self):
        del self.record["history"]
        del self.record["half_day"]
        result = self.edit()
        query, update = self.db.attendance_logs.update_one.call_args.args
        self.assertEqual(query["history"], {"$exists": False})
        self.assertEqual(query["half_day"], {"$exists": False})
        self.assertEqual(len(result["history"]), 1)
        self.assertEqual(set(update), {"$set", "$push"})

    def test_conditional_write_losers_return_409_for_both_endpoints(self):
        self.db.attendance_logs.update_one.return_value.matched_count = 0
        self.assert_error(409, lambda: self.close(datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc)))
        self.assert_error(409, self.edit)

    def test_unrelated_database_write_errors_propagate(self):
        self.db.attendance_logs.update_one.side_effect = RuntimeError("unavailable")
        for action in (self.edit, lambda: self.close(datetime(2026, 7, 6, 13, 0, tzinfo=timezone.utc))):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                action()
