"""Production listing/readiness functions; MongoDB calls are mocked."""
from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import MagicMock, call, patch

from test_calculation_helpers import load_helpers
from test_request_validation import HAS_HTTP
from test_shift_configuration import HTTPExceptionStub


class PyMongoErrorStub(Exception):
    pass


class ListingAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_list_endpoints=True)
        self.db = MagicMock()
        self.patch = patch.dict(self.code, db=self.db, HTTPException=HTTPExceptionStub,
                               PyMongoError=PyMongoErrorStub)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def cursor(self, collection, docs):
        cursor = MagicMock()
        cursor.sort.return_value = cursor
        cursor.skip.return_value = cursor
        cursor.limit.return_value = docs
        # Iterating the unpaginated cursor would expose a regression.
        cursor.__iter__.side_effect = AssertionError("Iteration before database pagination")
        collection.find.return_value = cursor
        return cursor

    def test_employee_defaults_filtered_total_and_database_sort(self):
        docs = [{"emp_code": "EMP0001", "created_at": datetime(2026, 7, 6, 4, 10)}]
        cursor = self.cursor(self.db.employees, docs)
        self.db.employees.count_documents.return_value = 27
        response = self.code["list_employees"](department="Engineering")
        query = {"department": "Engineering"}
        self.db.employees.find.assert_called_once_with(query, {"_id": 0})
        self.db.employees.count_documents.assert_called_once_with(query)
        self.assertEqual(cursor.method_calls, [call.sort([("emp_code", 1)]), call.skip(0), call.limit(20)])
        self.assertEqual((response["total"], response["page"], response["page_size"]), (27, 1, 20))
        self.assertEqual(response["items"][0]["created_at"], 1783311000000)

    def test_employee_filter_omission_empty_string_and_case_preserved(self):
        for value, query in [(None, {}), ("", {"department": ""}),
                             ("engineering", {"department": "engineering"})]:
            with self.subTest(value=value):
                self.db.reset_mock()
                self.cursor(self.db.employees, [])
                self.code["list_employees"](department=value)
                self.db.employees.find.assert_called_once_with(query, {"_id": 0})
                self.db.employees.count_documents.assert_called_once_with(query)

    def test_both_listings_second_page_and_valid_page_size_boundaries(self):
        for endpoint, collection in [("list_employees", self.db.employees),
                                     ("list_attendance", self.db.attendance_logs)]:
            for size in (1, 20, 100):
                with self.subTest(endpoint=endpoint, size=size):
                    cursor = self.cursor(collection, [])
                    self.code[endpoint](page=2, page_size=size)
                    cursor.skip.assert_called_once_with(size)
                    cursor.limit.assert_called_once_with(size)

    def test_invalid_pagination_rejected_before_database_access(self):
        for endpoint in ("list_employees", "list_attendance"):
            for values, field in [({"page": 0}, "page"), ({"page": -1}, "page"),
                                  ({"page_size": 0}, "page_size"),
                                  ({"page_size": -1}, "page_size"),
                                  ({"page_size": 101}, "page_size")]:
                with self.subTest(endpoint=endpoint, values=values):
                    self.db.reset_mock()
                    with self.assertRaises(HTTPExceptionStub) as raised:
                        self.code[endpoint](**values)
                    self.assertEqual(raised.exception.status_code, 422)
                    self.assertEqual(raised.exception.detail[0]["loc"], ["query", field])
                    self.assertEqual(self.db.mock_calls, [])

    def test_attendance_inclusive_filters_total_and_database_pagination(self):
        cursor = self.cursor(self.db.attendance_logs, [])
        self.db.attendance_logs.count_documents.return_value = 35
        response = self.code["list_attendance"](
            emp_code="EMP0001", date_from="2026-07-01", date_to="2026-07-31",
            status="WFH", page=2, page_size=10)
        query = {"emp_code": "EMP0001", "date": {"$gte": "2026-07-01", "$lte": "2026-07-31"}, "status": "WFH"}
        self.db.attendance_logs.find.assert_called_once_with(query, {"_id": 0}, hint={"date": -1, "emp_code": 1})
        self.db.attendance_logs.count_documents.assert_called_once_with(query)
        self.assertEqual(cursor.method_calls, [
            call.sort([("date", -1), ("emp_code", 1)]), call.skip(10), call.limit(10)])
        self.assertEqual((response["total"], response["page"], response["page_size"]), (35, 2, 10))

    def test_attendance_single_bounds_equal_bounds_and_leap_date(self):
        cases = [
            ({"date_from": "2024-02-29"}, {"$gte": "2024-02-29"}),
            ({"date_to": "2026-07-06"}, {"$lte": "2026-07-06"}),
            ({"date_from": "2026-07-06", "date_to": "2026-07-06"},
             {"$gte": "2026-07-06", "$lte": "2026-07-06"}),
        ]
        for values, bounds in cases:
            with self.subTest(values=values):
                self.db.reset_mock()
                self.cursor(self.db.attendance_logs, [])
                self.code["list_attendance"](**values)
                self.db.attendance_logs.find.assert_called_once_with(
                    {"date": bounds}, {"_id": 0}, hint={"date": -1, "emp_code": 1})

    def test_attendance_invalid_dates_status_and_reversed_range(self):
        cases = [{field: value} for field in ("date_from", "date_to") for value in (
            "", "2026-02-30", "2025-02-29", "2026-13-01", "0000-01-01",
            "20260706", "2026-7-06", "2026-W28-1", "2026-07-06T00:00:00Z", "invalid")]
        cases += [{"status": value} for value in ("", "UNKNOWN", "present")]
        cases += [{"date_from": "2026-07-07", "date_to": "2026-07-06"}]
        for values in cases:
            with self.subTest(values=values):
                self.db.reset_mock()
                with self.assertRaises(HTTPExceptionStub) as raised:
                    self.code["list_attendance"](**values)
                self.assertEqual(raised.exception.status_code, 422)
                self.assertIsInstance(raised.exception.detail, list)
                self.assertEqual(self.db.mock_calls, [])

    def test_attendance_all_statuses_and_empty_employee_code(self):
        for status in ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"):
            with self.subTest(status=status):
                self.db.reset_mock()
                self.cursor(self.db.attendance_logs, [])
                self.code["list_attendance"](emp_code="", status=status)
                self.db.attendance_logs.find.assert_called_once_with(
                    {"emp_code": "", "status": status}, {"_id": 0}, hint={"date": -1, "emp_code": 1})

    def test_attendance_defaults_legacy_records_without_mutating_documents(self):
        documents = [
            {"_id": "internal", "emp_code": "EMP0001", "date": "2026-07-06", "status": "ABSENT"},
            {"emp_code": "EMP0002", "date": "2026-07-06", "status": "LEAVE", "punch_in": None,
             "punch_out": None, "work_hours": None},
            {"emp_code": "EMP0003", "date": "2026-07-06", "status": "WFH",
             "punch_in": datetime(2026, 7, 6, 4, 10), "punch_out": None,
             "work_hours": None, "late_minutes": 10, "overtime_minutes": 40,
             "half_day": True, "history": [{"at": datetime(2026, 7, 7, 4, 10),
                                            "by": "hr", "reason": "correction", "changes": {}}]},
        ]
        original = deepcopy(documents)
        self.cursor(self.db.attendance_logs, documents)
        rows = self.code["list_attendance"]()["items"]
        required = {"emp_code", "date", "status", "punch_in", "punch_out", "work_hours",
                    "late_minutes", "overtime_minutes", "half_day", "history"}
        for row in rows:
            self.assertEqual(set(row), required)
        for row in rows[:2]:
            self.assertIsNone(row["punch_in"])
            self.assertIsNone(row["punch_out"])
            self.assertIsNone(row["work_hours"])
            self.assertEqual((row["late_minutes"], row["overtime_minutes"], row["half_day"], row["history"]),
                             (0, 0, False, []))
        self.assertEqual(rows[2]["punch_in"], 1783311000000)
        self.assertEqual((rows[2]["late_minutes"], rows[2]["overtime_minutes"], rows[2]["half_day"]), (10, 40, True))
        self.assertIsInstance(rows[2]["history"][0]["at"], int)
        self.assertEqual(documents, original)

    def test_health_pings_database_before_success(self):
        self.db.command.return_value = {"ok": 1}
        self.assertEqual(self.code["health"](), {"status": "ok"})
        self.db.command.assert_called_once_with("ping")

    def test_health_ping_failure_returns_503(self):
        self.db.command.side_effect = PyMongoErrorStub("unreachable")
        with self.assertRaises(HTTPExceptionStub) as raised:
            self.code["health"]()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail, "MongoDB unavailable")


@unittest.skipUnless(HAS_HTTP, "FastAPI/Pydantic/httpx missing: listing HTTP validation unverified")
class ListingHTTPTests(unittest.TestCase):
    def test_query_types_and_bounds_return_422_without_database_access(self):
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient
        code = load_helpers(include_list_endpoints=True)
        app = FastAPI()
        app.get("/employees")(code["list_employees"])
        app.get("/attendance")(code["list_attendance"])
        db = MagicMock()
        with patch.dict(code, db=db, HTTPException=HTTPException), TestClient(app) as client:
            for path in ("/employees", "/attendance"):
                for query in ({"page": "invalid"}, {"page": 0}, {"page_size": 0}, {"page_size": 101}):
                    with self.subTest(path=path, query=query):
                        response = client.get(path, params=query)
                        self.assertEqual(response.status_code, 422)
                        self.assertIsInstance(response.json()["detail"], list)
                        self.assertEqual(db.mock_calls, [])
            for query in ({"date_from": ""}, {"date_to": "2026-02-30"}, {"status": ""},
                          {"date_from": "2026-07-07", "date_to": "2026-07-06"}):
                with self.subTest(query=query):
                    response = client.get("/attendance", params=query)
                    self.assertEqual(response.status_code, 422)
                    self.assertIsInstance(response.json()["detail"], list)
                    self.assertEqual(db.mock_calls, [])
