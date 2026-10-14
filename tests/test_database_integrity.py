"""Production startup and write paths with database/exception test doubles."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import load_helpers
from test_shift_configuration import HTTPExceptionStub


class DuplicateKeyErrorStub(Exception):
    pass


class DatabaseIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_write_endpoints=True, include_startup=True)
        self.db = MagicMock()
        self.db.employees.find_one.return_value = None
        self.db.attendance_logs.find_one.return_value = None
        self.patch = patch.dict(self.code, db=self.db, HTTPException=HTTPExceptionStub,
                               DuplicateKeyError=DuplicateKeyErrorStub)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_startup_requests_same_unique_indexes_on_each_initialization(self):
        async def initialize_twice():
            for _ in range(2):
                async with self.code["lifespan"](None):
                    pass
        asyncio.run(initialize_twice())
        self.assertEqual(self.db.employees.create_index.call_count, 6)
        self.assertEqual(self.db.attendance_logs.create_index.call_count, 4)
        for call in self.db.employees.create_index.call_args_list[::3]:
            self.assertEqual(call.args, ([("emp_code", 1)],))
            self.assertEqual(call.kwargs, {"unique": True})
        for call in self.db.attendance_logs.create_index.call_args_list[::2]:
            self.assertEqual(call.args, ([("emp_code", 1), ("date", 1)],))
            self.assertEqual(call.kwargs, {"unique": True})
        for call in self.db.employees.create_index.call_args_list[1::3]:
            self.assertEqual(call.args, ([("department", 1), ("joined_on", 1), ("emp_code", 1)],))
            self.assertEqual(call.kwargs, {})
        for call in self.db.employees.create_index.call_args_list[2::3]:
            self.assertEqual(call.args, ([("joined_on", 1), ("department", 1), ("emp_code", 1)],))
        for call in self.db.attendance_logs.create_index.call_args_list[1::2]:
            self.assertEqual(call.args, ([("date", -1), ("emp_code", 1)],))
            self.assertEqual(call.kwargs, {})

    def test_index_failure_prevents_startup(self):
        self.db.employees.create_index.side_effect = RuntimeError("index failed")
        async def initialize():
            async with self.code["lifespan"](None):
                self.fail("Startup must fail if uniqueness cannot be established")
        with self.assertRaisesRegex(RuntimeError, "index failed"):
            asyncio.run(initialize())

    def test_unknown_employee_stops_before_attendance_access(self):
        with self.assertRaises(HTTPExceptionStub) as raised:
            self.code["punch_in"](SimpleNamespace(emp_code="EMP9999"))
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.db.attendance_logs.mock_calls, [])

    def write_cases(self):
        fields = {"emp_code": "EMP0001", "shift_start": "09:30", "shift_end": "18:30"}
        return [
            ("create_employee", "employees", SimpleNamespace(**fields, model_dump=lambda: dict(fields))),
            ("punch_in", "attendance_logs", SimpleNamespace(
                emp_code="EMP0001", punched_at=1783311000900, status="PRESENT")),
        ]

    def test_duplicate_insert_after_successful_precheck_returns_409(self):
        for name, collection, body in self.write_cases():
            with self.subTest(endpoint=name):
                self.db.reset_mock(side_effect=True)
                self.db.employees.find_one.return_value = (
                    None if name == "create_employee" else {"shift_start": "09:30", "shift_end": "18:30"})
                getattr(self.db, collection).insert_one.side_effect = DuplicateKeyErrorStub("duplicate")
                with self.assertRaises(HTTPExceptionStub) as raised:
                    self.code[name](body)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertIsInstance(raised.exception.detail, str)

    def test_unrelated_insert_failures_propagate(self):
        for name, collection, body in self.write_cases():
            with self.subTest(endpoint=name):
                self.db.reset_mock(side_effect=True)
                self.db.employees.find_one.return_value = (
                    None if name == "create_employee" else {"shift_start": "09:30", "shift_end": "18:30"})
                failure = RuntimeError("database unavailable")
                getattr(self.db, collection).insert_one.side_effect = failure
                with self.assertRaises(RuntimeError) as raised:
                    self.code[name](body)
                self.assertIs(raised.exception, failure)

    def test_existing_duplicate_prechecks_remain_409(self):
        for name, collection, body in self.write_cases():
            with self.subTest(endpoint=name):
                self.db.reset_mock(side_effect=True)
                self.db.employees.find_one.return_value = {"shift_start": "09:30", "shift_end": "18:30"}
                self.db.attendance_logs.find_one.return_value = {"date": "2026-07-06"}
                with self.assertRaises(HTTPExceptionStub) as raised:
                    self.code[name](body)
                self.assertEqual(raised.exception.status_code, 409)
                getattr(self.db, collection).insert_one.assert_not_called()
