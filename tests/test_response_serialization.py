"""Response conversion checks using production definitions and mocked MongoDB."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import IST, load_helpers


class ResponseSerializationTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_write_endpoints=True, include_list_endpoints=True)

    def test_nested_timestamps_preserve_other_values_and_source(self):
        naive = datetime(2026, 7, 6, 4, 10, 0, 900000)
        aware = datetime(2026, 7, 6, 9, 40, 0, 900000, tzinfo=IST)
        source = {
            "created_at": naive, "punch_in": aware, "punch_out": None,
            "date": "2026-07-06", "joined_on": "2026-01-05", "shift_start": "09:30",
            "work_hours": 1.13, "late_minutes": 10, "half_day": False,
            "history": [{"at": aware, "by": "hr.admin", "reason": "fix punch",
                         "changes": {
                             "punch_in": {"from": naive, "to": aware},
                             "punch_out": {"from": None, "to": aware},
                             "status": {"from": "WFH", "to": "PRESENT"},
                             "late_minutes": {"from": 0, "to": 10},
                         }}],
        }
        original = deepcopy(source)
        expected = deepcopy(source)
        expected.update(created_at=1783311000000, punch_in=1783311000000)
        expected["history"][0]["at"] = 1783311000000
        changes = expected["history"][0]["changes"]
        changes["punch_in"] = {"from": 1783311000000, "to": 1783311000000}
        changes["punch_out"] = {"from": None, "to": 1783311000000}
        result = self.code["serialize_response"](source)
        self.assertEqual(result, expected)
        self.assertIs(type(result["punch_in"]), int)
        self.assertIs(type(result["half_day"]), bool)
        self.assertEqual(source, original)

    def test_already_serialized_timestamps_are_not_converted_again(self):
        source = {"punch_in": 1783311000000, "punch_out": None,
                  "history": [{"at": 1783311000000,
                               "changes": {"punch_in": {"from": None, "to": 1783311000000}}}]}
        serialize = self.code["serialize_response"]
        self.assertEqual(serialize(source), source)
        self.assertEqual(serialize(serialize(source)), source)

    def test_create_and_punch_responses_leave_written_datetimes_intact(self):
        db = MagicMock()
        db.employees.find_one.side_effect = [None, {"shift_start": "09:30", "shift_end": "18:30"}]
        db.attendance_logs.find_one.return_value = None
        employee = MagicMock()
        employee.model_dump.return_value = {"emp_code": "EMP0001", "joined_on": "2026-01-05"}
        with patch.dict(self.code, db=db):
            created = self.code["create_employee"](employee)
            punched = self.code["punch_in"](SimpleNamespace(
                emp_code="EMP0001", punched_at=1783311000900, status="PRESENT"
            ))
        stored_employee = db.employees.insert_one.call_args.args[0]
        stored_log = db.attendance_logs.insert_one.call_args.args[0]
        self.assertIsInstance(stored_employee["created_at"], datetime)
        self.assertIsInstance(stored_log["punch_in"], datetime)
        self.assertIs(type(created["created_at"]), int)
        self.assertEqual(created["created_at"], self.code["serialize_response"](
            stored_employee["created_at"]
        ))
        self.assertEqual(created["joined_on"], "2026-01-05")
        self.assertEqual(punched["punch_in"], 1783311000000)
        self.assertIsNone(punched["punch_out"])
        self.assertEqual(punched["late_minutes"], stored_log["late_minutes"])
        self.assertEqual(punched["date"], stored_log["date"])

    def test_both_list_responses_serialize_stored_and_nested_datetimes(self):
        instant = datetime(2026, 7, 6, 4, 10, 0, 900000, tzinfo=timezone.utc)
        db = MagicMock()
        db.employees.count_documents.return_value = 1
        db.employees.find.return_value.sort.return_value.skip.return_value.limit.return_value = [
            {"emp_code": "EMP0001", "created_at": instant}
        ]
        db.attendance_logs.count_documents.return_value = 1
        db.attendance_logs.find.return_value.sort.return_value.skip.return_value.limit.return_value = [{
            "_id": "internal", "emp_code": "EMP0001", "date": "2026-07-06",
            "punch_in": instant, "punch_out": None,
            "history": [{"at": instant, "changes": {
                "punch_in": {"from": None, "to": instant}
            }}],
        }]
        with patch.dict(self.code, db=db):
            employees = self.code["list_employees"]()
            attendance = self.code["list_attendance"]()
        self.assertEqual(employees["items"][0]["created_at"], 1783311000000)
        record = attendance["items"][0]
        self.assertEqual(record["punch_in"], 1783311000000)
        self.assertIsNone(record["punch_out"])
        self.assertEqual(record["history"][0]["at"], 1783311000000)
        self.assertEqual(record["history"][0]["changes"]["punch_in"]["to"], 1783311000000)
        for response in [employees, attendance]:
            self.assertEqual((response["total"], response["page"], response["page_size"]), (1, 1, 20))


if __name__ == "__main__":
    unittest.main()
