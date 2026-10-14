"""Real model/HTTP tests when available; isolated date checks otherwise."""
import ast
from datetime import datetime
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import load_helpers


SOURCE = Path(__file__).resolve().parents[1] / "app" / "main.py"
HAS_PYDANTIC = importlib.util.find_spec("pydantic") is not None
HAS_HTTP = HAS_PYDANTIC and all(
    importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx")
)
EMPLOYEE = {
    "emp_code": "EMP0001", "name": "Asha Rao", "email": "asha@example.com",
    "department": "Engineering", "joined_on": "2026-01-05",
}
INVALID_EMPLOYEES = [
    ("emp_code", value) for value in ("abc", "EMP123", "EMP1234567", "EMP0001x", None, 123)
] + [
    ("name", ""), ("name", "n" * 101), ("department", ""),
    ("department", "d" * 51), ("email", "bad"), ("email", "a b@example.com"),
    ("email", "a@b@c.com"), ("email", "a" * 115 + "@b.com"),
    ("joined_on", "2026-02-30"), ("joined_on", "2025-02-29"),
    ("joined_on", "2026-13-01"), ("joined_on", "0000-01-01"),
    ("joined_on", "20260706"), ("joined_on", "2026-7-06"),
    ("joined_on", "2026-07-06T00:00:00Z"),
] + [(field, value) for field in ("name", "email", "department", "joined_on")
     for value in (None, 123, True, [], {})
] + [(field, value) for field in ("shift_start", "shift_end")
     for value in ("9:30", "24:00", "09:60", "09:30:00", "invalid", None)]
INVALID_EPOCHS = [None, "1783311000900", True, False, 1783311000900.0,
                  1783311000900.5, -1, 1783311000, 99999999999,
                  4102444800001, 10**100, [], {}]


def load_models():
    """Compile actual production model definitions with real Pydantic."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if (
        isinstance(node, ast.ImportFrom) and node.module in {"datetime", "typing", "pydantic"}
    ) or (isinstance(node, ast.ClassDef) and node.name in {
        "EmployeeIn", "PunchInIn", "PunchOutIn", "RegularizeIn"})]
    namespace = {"__name__": "validation_models"}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


class CalendarValidatorTests(unittest.TestCase):
    def test_actual_calendar_validator(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        employee = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                        and node.name == "EmployeeIn")
        validator = next(node for node in employee.body if isinstance(node, ast.FunctionDef)
                         and node.name == "validate_joined_on")
        validator.decorator_list = []
        namespace = {"datetime": datetime}
        exec(compile(ast.Module(body=[validator], type_ignores=[]), str(SOURCE), "exec"), namespace)
        validate = namespace["validate_joined_on"]
        for value in ("2024-02-29", "2026-01-05", "2026-12-31"):
            self.assertEqual(validate(None, value), value)
        for value in ("2025-02-29", "2026-02-30", "2026-13-01", "0000-01-01"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(None, value)


@unittest.skipUnless(HAS_PYDANTIC, "Pydantic missing: real model validation unverified")
class RequestModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from pydantic import ValidationError
        cls.error = ValidationError
        cls.models = load_models()

    def test_employee_defaults_and_valid_boundaries(self):
        model = self.models["EmployeeIn"](**EMPLOYEE)
        self.assertEqual((model.shift_start, model.shift_end), ("09:30", "18:30"))
        for changes in (
            {"name": "n", "department": "d"},
            {"emp_code": "EMP123456", "name": "n" * 100, "department": "d" * 50},
            {"email": "a" * 114 + "@b.com", "joined_on": "2024-02-29"},
            {"shift_start": "22:00", "shift_end": "06:00"},
            {"shift_start": "00:00", "shift_end": "23:59"},
        ):
            with self.subTest(changes=changes):
                self.models["EmployeeIn"](**(EMPLOYEE | changes))

    def test_invalid_employee_fields(self):
        for field, value in INVALID_EMPLOYEES:
            with self.subTest(field=field, value=value), self.assertRaises(self.error):
                self.models["EmployeeIn"](**(EMPLOYEE | {field: value}))

    def test_required_employee_fields(self):
        for field in EMPLOYEE:
            payload = dict(EMPLOYEE)
            del payload[field]
            with self.subTest(field=field), self.assertRaises(self.error):
                self.models["EmployeeIn"](**payload)

    def test_epoch_inclusive_bounds_and_omission(self):
        model = self.models["PunchInIn"](emp_code="EMP0001")
        self.assertIsNone(model.punched_at)
        self.assertEqual(model.status, "PRESENT")
        for value in (100000000000, 1783311000900, 4102444800000):
            for name in ("PunchInIn", "PunchOutIn"):
                self.assertEqual(self.models[name](emp_code="EMP0001", punched_at=value).punched_at, value)
            correction = self.models["RegularizeIn"](
                reason="fix punch", regularized_by="hr", punch_in=value, punch_out=value)
            self.assertEqual((correction.punch_in, correction.punch_out), (value, value))
        self.assertIsNone(self.models["PunchOutIn"](emp_code="EMP0001").punched_at)
        self.assertEqual(self.models["RegularizeIn"](reason="fix punch", regularized_by="hr").model_dump(
            exclude_unset=True), {"reason": "fix punch", "regularized_by": "hr"})

    def test_invalid_epoch_types_and_range(self):
        for value in INVALID_EPOCHS:
            for name in ("PunchInIn", "PunchOutIn"):
                with self.subTest(model=name, value=value), self.assertRaises(self.error):
                    self.models[name](emp_code="EMP0001", punched_at=value)
            for field in ("punch_in", "punch_out"):
                with self.subTest(field=field, value=value), self.assertRaises(self.error):
                    self.models["RegularizeIn"](reason="fix punch", regularized_by="hr", **{field: value})

    def test_presence_status_and_required_employee_code(self):
        for value in ("PRESENT", "WFH", "ON_DUTY"):
            self.assertEqual(self.models["PunchInIn"](emp_code="EMP0001", status=value).status, value)
        for value in ("ABSENT", "LEAVE", "present", "UNKNOWN", None, 1):
            with self.subTest(value=value), self.assertRaises(self.error):
                self.models["PunchInIn"](emp_code="EMP0001", status=value)
        with self.assertRaises(self.error):
            self.models["PunchInIn"]()
        with self.assertRaises(self.error):
            self.models["PunchOutIn"]()
        for status in ("PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"):
            self.models["RegularizeIn"](reason="fix punch", regularized_by="hr", status=status)
        for status in (None, "UNKNOWN", "present"):
            with self.assertRaises(self.error):
                self.models["RegularizeIn"](reason="fix punch", regularized_by="hr", status=status)
        for payload in ({}, {"reason": "valid reason"}, {"regularized_by": "hr"},
                        {"reason": "four", "regularized_by": "hr"},
                        {"reason": "r" * 201, "regularized_by": "hr"},
                        {"reason": "valid reason", "regularized_by": ""},
                        {"reason": "valid reason", "regularized_by": "x" * 51}):
            with self.subTest(payload=payload), self.assertRaises(self.error):
                self.models["RegularizeIn"](**payload)
        for reason, actor in (("r" * 5, "x"), ("r" * 200, "x" * 50)):
            self.models["RegularizeIn"](reason=reason, regularized_by=actor)
        correction = self.models["RegularizeIn"](reason="valid reason", regularized_by="hr", work_hours=99)
        self.assertNotIn("work_hours", correction.model_dump(exclude_unset=True))


@unittest.skipUnless(HAS_HTTP, "FastAPI/Pydantic/httpx missing: HTTP validation unverified")
class RequestHTTPTests(unittest.TestCase):
    def test_invalid_bodies_return_422_without_database_access(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        models = load_models()
        code = load_helpers(include_write_endpoints=True)
        app = FastAPI()
        for path, name, model_name in (
            ("/employees", "create_employee", "EmployeeIn"),
            ("/attendance/punch-in", "punch_in", "PunchInIn"),
            ("/attendance/punch-out", "punch_out", "PunchOutIn"),
        ):
            code[name].__annotations__["body"] = models[model_name]
            app.post(path, status_code=200 if name == "punch_out" else 201)(code[name])
        code["regularize_attendance"].__annotations__["body"] = models["RegularizeIn"]
        app.patch("/attendance/{emp_code}/{date}")(code["regularize_attendance"])
        db = MagicMock()
        cases = [("/employees", EMPLOYEE | {field: value})
                 for field, value in INVALID_EMPLOYEES]
        cases += [("/employees", EMPLOYEE | {"shift_start": "06:00", "shift_end": "06:00"})]
        cases += [("/employees", {key: value for key, value in EMPLOYEE.items() if key != field})
                  for field in EMPLOYEE]
        cases += [("/attendance/punch-in", {"emp_code": "EMP0001", "punched_at": value})
                  for value in INVALID_EPOCHS]
        cases += [("/attendance/punch-in", {"emp_code": "EMP0001", "status": value})
                  for value in ("ABSENT", "LEAVE", "UNKNOWN", None)]
        cases += [("/attendance/punch-in", {})]
        cases += [("/attendance/punch-out", {"emp_code": "EMP0001", "punched_at": value})
                  for value in INVALID_EPOCHS]
        cases += [("/attendance/punch-out", {})]
        with patch.dict(code, db=db, HTTPException=HTTPException), TestClient(app) as client:
            for path, payload in cases:
                with self.subTest(path=path, payload=payload):
                    response = client.post(path, json=payload)
                    self.assertEqual(response.status_code, 422)
                    self.assertIsInstance(response.json()["detail"], list)
                    self.assertEqual(db.mock_calls, [])
            for payload in ({}, {"reason": "four", "regularized_by": "hr"},
                            {"reason": "valid reason", "regularized_by": ""},
                            {"reason": "valid reason", "regularized_by": "hr", "punch_in": None},
                            {"reason": "valid reason", "regularized_by": "hr", "punch_out": "1783311000900"}):
                response = client.patch("/attendance/EMP0001/2026-07-06", json=payload)
                self.assertEqual(response.status_code, 422)
                self.assertIsInstance(response.json()["detail"], list)
                self.assertEqual(db.mock_calls, [])
