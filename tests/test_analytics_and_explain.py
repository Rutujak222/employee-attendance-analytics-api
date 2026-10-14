"""Production builders/expressions and endpoint wiring, not a MongoDB emulator.

Expression checks implement only the documented operators used by the tested
expressions. They cannot establish server support, plans, joins or concurrency.
"""
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from test_calculation_helpers import IST, load_helpers
from test_request_validation import HAS_HTTP
from test_shift_configuration import HTTPExceptionStub


def expression(value, document=None, variables=None):
    document, variables = document or {}, variables or {}
    if isinstance(value, str) and value.startswith("$"):
        path = value[2:] if value.startswith("$$") else value[1:]
        result = variables if value.startswith("$$") else document
        for key in path.split("."):
            result = result.get(key) if isinstance(result, dict) else None
        return result
    if isinstance(value, list):
        return [expression(item, document, variables) for item in value]
    if not isinstance(value, dict):
        return value
    if len(value) != 1 or not next(iter(value)).startswith("$"):
        return {key: expression(item, document, variables) for key, item in value.items()}
    operator, argument = next(iter(value.items()))
    evaluate = lambda item: expression(item, document, variables)
    if operator == "$cond":
        return evaluate(argument[1] if evaluate(argument[0]) else argument[2])
    if operator == "$ifNull":
        first = evaluate(argument[0])
        return evaluate(argument[1]) if first is None else first
    if operator == "$filter":
        return [item for item in evaluate(argument["input"]) if expression(
            argument["cond"], document, variables | {argument["as"]: item})]
    if operator == "$size":
        return len(evaluate(argument))
    if operator == "$range":
        return list(range(*evaluate(argument)))
    if operator == "$dateFromString":
        return datetime.strptime(evaluate(argument["dateString"]), "%Y-%m-%d").replace(tzinfo=IST)
    if operator == "$dateAdd":
        return evaluate(argument["startDate"]) + timedelta(days=evaluate(argument["amount"]))
    if operator == "$dateToString":
        return evaluate(argument["date"]).astimezone(IST).strftime("%Y-%m-%d")
    if operator == "$dayOfWeek":
        return (evaluate(argument["date"]).astimezone(IST).weekday() + 1) % 7 + 1
    if operator == "$toDecimal":
        return Decimal(str(evaluate(argument)))
    if operator == "$floor":
        return evaluate(argument).to_integral_value(rounding=ROUND_FLOOR)
    arguments = evaluate(argument)
    operations = {
        "$eq": lambda: arguments[0] == arguments[1], "$ne": lambda: arguments[0] != arguments[1],
        "$gt": lambda: arguments[0] > arguments[1], "$gte": lambda: arguments[0] >= arguments[1],
        "$lte": lambda: arguments[0] <= arguments[1], "$in": lambda: arguments[0] in arguments[1],
        "$and": lambda: all(arguments), "$add": lambda: sum(arguments),
        "$multiply": lambda: arguments[0] * arguments[1], "$divide": lambda: arguments[0] / arguments[1],
    }
    if operator not in operations:
        raise AssertionError("Unsupported expression: " + operator)
    return operations[operator]()


class AnalyticsAndExplainTests(unittest.TestCase):
    def setUp(self):
        self.code = load_helpers(include_analytics=True, include_list_endpoints=True)
        self.db = MagicMock()
        self.patch = patch.dict(self.code, db=self.db, HTTPException=HTTPExceptionStub,
                               json_util=SimpleNamespace(dumps=json.dumps))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def assert_error(self, status, function):
        with self.assertRaises(HTTPExceptionStub) as raised:
            function()
        self.assertEqual(raised.exception.status_code, status)

    def test_month_bounds_leap_year_and_invalid_months_before_database_access(self):
        self.assertEqual(self.code["month_bounds"]("2024-02"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(self.code["month_bounds"]("2026-12"), ("2026-12-01", "2026-12-31"))
        for month in (None, "", "2026-2", "2026-00", "2026-13", "0000-01", "202602", "2026-07-01"):
            for endpoint in (lambda: self.code["employee_monthly"]("EMP0001", month),
                             lambda: self.code["department_summary"](month),
                             lambda: self.code["late_leaderboard"](month)):
                self.assert_error(422, endpoint)
        self.assertEqual(self.db.mock_calls, [])

    def test_employee_working_days_from_join_date_including_zero_days(self):
        operation = self.code["build_employee_monthly"]("EMP0001", "2026-07")
        working = operation["pipeline"][2]["$set"]["working_days"]
        for joined, expected in [("2026-01-01", 23), ("2026-07-16", 12),
                                 ("2026-07-04", 20), ("2026-08-01", 0)]:
            with self.subTest(joined=joined):
                self.assertEqual(expression(working, {"joined_on": joined}), expected)

    def test_stored_metrics_weekends_half_days_missing_fields_and_record_weighted_work(self):
        rows = [
            {"date": "2026-07-06", "status": "PRESENT", "work_hours": 1.125, "late_minutes": 10},
            {"date": "2026-07-07", "status": "WFH", "half_day": True, "work_hours": 2.125},
            {"date": "2026-07-11", "status": "ON_DUTY", "work_hours": 3.125, "late_minutes": 20, "overtime_minutes": 40},
            {"date": "2026-07-08", "status": "LEAVE", "work_hours": 999},
            {"date": "2026-07-09", "status": "ABSENT"},
            {"date": "2026-07-10", "status": "PRESENT", "work_hours": None},
        ]
        group = self.code["monthly_metrics_group"]()["$group"]
        totals = {key: sum(expression(accumulator["$sum"], row) for row in rows)
                  for key, accumulator in group.items() if key != "_id"}
        self.assertEqual(totals, {"present_days": 2.5, "leave_days": 1, "late_count": 2,
                                 "total_late_minutes": 30, "total_overtime_minutes": 40,
                                 "on_duty_count": 1, "work_total": Decimal("6.375"), "work_count": 3})
        projection = self.code["build_department_summary"]("2026-07")["pipeline"][4]["$project"]
        self.assertEqual(expression(projection["avg_work_hours"], totals), Decimal("2.13"))
        self.assertIsNone(expression(projection["avg_work_hours"], {"work_total": 0, "work_count": 0}))
        text = json.dumps(group)
        self.assertNotIn("$punch_in", text)
        self.assertNotIn("$punch_out", text)

    def test_half_up_expression_boundaries_and_null(self):
        for value, places, expected in [("1.1249", 2, "1.12"), ("1.125", 2, "1.13"),
                                        ("1.1251", 2, "1.13"), ("0.12345", 4, "0.1235")]:
            with self.subTest(value=value):
                self.assertEqual(expression(self.code["mongo_half_up"]({"$toDecimal": value}, places)), Decimal(expected))
        self.assertIsNone(expression(self.code["mongo_half_up"](None)))

    def test_employee_percentage_and_zero_log_pipeline_defaults(self):
        operation = self.code["build_employee_monthly"]("EMP0001", "2026-07")
        self.assertEqual(operation["pipeline"][0], {"$match": {"emp_code": "EMP0001"}})
        project = operation["pipeline"][3]["$project"]
        self.assertEqual(expression(project["present_days"], {}), 0)
        percentage = operation["pipeline"][4]["$set"]["attendance_pct"]
        self.assertEqual(expression(percentage, {"present_days": 1, "working_days": 3}), Decimal("33.33"))
        self.assertIsNone(expression(percentage, {"present_days": 0, "working_days": 0}))
        row = {"emp_code": "EMP0001", "month": "2026-07", "working_days": 23, "present_days": 0,
               "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0, "attendance_pct": 0}
        self.db.employees.aggregate.return_value = [row]
        self.assertEqual(self.code["employee_monthly"]("EMP0001", "2026-07"), row)
        self.db.employees.aggregate.assert_called_once_with(operation["pipeline"], hint=operation["hint"], allowDiskUse=True)
        self.db.employees.aggregate.return_value = []
        self.assert_error(404, lambda: self.code["employee_monthly"]("EMP9999", "2026-07"))

    def test_department_headcount_starts_with_employees_and_keeps_zero_log_people(self):
        operation = self.code["build_department_summary"]("2026-07", "")
        pipeline = operation["pipeline"]
        self.assertEqual(operation["aggregate"], "employees")
        self.assertEqual(pipeline[0], {"$match": {"joined_on": {"$lte": "2026-07-31"}, "department": ""}})
        self.assertEqual(pipeline[3]["$group"]["headcount"], {"$sum": 1})
        self.assertNotIn("$unwind", [key for stage in pipeline for key in stage])
        self.assertEqual(pipeline[-1], {"$sort": {"department": 1}})
        self.db.employees.aggregate.return_value = []
        self.assertEqual(self.code["department_summary"]("2026-07", ""), {"month": "2026-07", "items": []})

    def test_leaderboard_competition_rank_department_filter_and_tie_cutoff(self):
        pipeline = self.code["build_late_leaderboard"]("2026-07", 2, "Engineering")["pipeline"]
        ranking = next(i for i, stage in enumerate(pipeline) if "$setWindowFields" in stage)
        self.assertEqual(pipeline[ranking], {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}})
        self.assertIn({"$match": {"employee.department": "Engineering"}}, pipeline[:ranking])
        self.assertEqual(pipeline[ranking + 1], {"$match": {"rank": {"$lte": 2}}})
        self.assertEqual(pipeline[-1], {"$sort": {"total_late_minutes": -1, "emp_code": 1}})
        self.assertNotIn("$limit", [key for stage in pipeline for key in stage])
        self.assertIn({"$unwind": "$employee"}, pipeline)  # Drops orphan logs.
        rows = [{"rank": rank, "emp_code": code, "name": code, "department": "Engineering",
                 "total_late_minutes": minutes, "late_count": 1}
                for rank, code, minutes in [(1, "EMP0001", 50), (2, "EMP0002", 40), (2, "EMP0003", 40)]]
        self.db.attendance_logs.aggregate.return_value = rows
        self.assertEqual(len(self.code["late_leaderboard"]("2026-07", 2)["items"]), 3)
        for limit in (0, 51):
            self.assert_error(422, lambda: self.code["late_leaderboard"]("2026-07", limit))
        self.code["build_late_leaderboard"]("2026-07", 1)
        self.code["build_late_leaderboard"]("2026-07", 50)
        self.db.attendance_logs.aggregate.return_value = []
        self.assertEqual(self.code["late_leaderboard"]("2026-07"), {"month": "2026-07", "items": []})

    def test_trend_range_validation_and_database_gap_generation(self):
        self.assertEqual(self.code["trend_bounds"]("2026-01-01", "2026-04-02"), 92)
        self.assertEqual(self.code["trend_bounds"]("2026-07-06", "2026-07-06"), 1)
        for from_, to in [(None, "2026-07-06"), ("", "2026-07-06"), ("2026-07-06", None),
                          ("2026-02-30", "2026-03-01"), ("2026-07-07", "2026-07-06"),
                          ("2026-01-01", "2026-04-03")]:
            self.assert_error(422, lambda: self.code["department_trend"]("Engineering", from_, to))
        self.assertEqual(self.db.mock_calls, [])
        pipeline = self.code["build_department_trend"]("Engineering", "2026-01-01", "2026-04-02")["pipeline"]
        self.assertEqual(pipeline[2]["$project"]["offset"], {"$range": [0, 92]})
        window = next(stage["$setWindowFields"] for stage in pipeline if "$setWindowFields" in stage)
        self.assertEqual(window["output"]["moving_avg_7d"], {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}})
        self.assertEqual(pipeline[-1], {"$sort": {"date": 1}})

    def test_trend_daily_headcount_weekends_zero_logs_and_null_rates(self):
        pipeline = self.code["build_department_trend"]("Engineering", "2026-07-06", "2026-07-12")["pipeline"]
        headcount = pipeline[6]["$set"]["headcount"]
        self.assertEqual(expression(headcount, {"date": "2026-07-06", "employees": [
            {"joined_on": "2026-07-06"}, {"joined_on": "2026-07-07"}]}), 1)
        rate = pipeline[10]["$set"]["attendance_rate"]
        for values, expected in [({"is_working_day": True, "headcount": 3, "present_count": 0.5}, Decimal("0.1667")),
                                 ({"is_working_day": True, "headcount": 2, "present_count": 0}, Decimal(0)),
                                 ({"is_working_day": False, "headcount": 2, "present_count": 1}, None),
                                 ({"is_working_day": True, "headcount": 0, "present_count": 0}, None)]:
            self.assertEqual(expression(rate, values), expected)
        self.db.employees.aggregate.return_value = []
        self.assert_error(404, lambda: self.code["department_trend"]("unknown", "2026-07-06", "2026-07-12"))

    def test_decimal_aggregation_results_become_json_numbers(self):
        class DecimalResult:
            def to_decimal(self):
                return Decimal("1.13")
        self.assertEqual(self.code["analytics_json"]({"items": [{"avg": DecimalResult(), "rate": None}]}),
                         {"items": [{"avg": 1.13, "rate": None}]})

    def test_explain_every_operation_is_identical_to_production_builder(self):
        self.db.command.return_value = {"executionStats": {"nReturned": 7, "totalKeysExamined": 13}}
        cases = [
            ("attendance_list", {"emp_code": "", "date_from": "2026-07-01", "date_to": "2026-07-31",
                                 "status": "WFH", "page": 2, "page_size": 10}, "build_attendance_list"),
            ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}, "build_employee_monthly"),
            ("department_summary", {"month": "2026-07", "department": ""}, "build_department_summary"),
            ("late_leaderboard", {"month": "2026-07", "limit": 2, "department": "Engineering"}, "build_late_leaderboard"),
            ("department_trend", {"department": "Engineering", "from_": "2026-07-06", "to": "2026-07-12"}, "build_department_trend"),
        ]
        for endpoint, arguments, builder in cases:
            with self.subTest(endpoint=endpoint):
                self.db.reset_mock()
                operation = self.code[builder](**arguments)
                response = self.code["explain_endpoint"](endpoint, **arguments)
                self.db.command.assert_called_once_with("explain", operation, verbosity="executionStats")
                self.assertEqual(response, {"endpoint": endpoint, "collection": operation.get("aggregate", operation.get("find")),
                                            "explain": self.db.command.return_value})

    def test_explain_missing_required_and_invalid_parameters_do_not_reach_database(self):
        cases = [("unknown", {}), ("employee_monthly", {}), ("employee_monthly", {"month": "2026-07"}),
                 ("department_summary", {}), ("late_leaderboard", {}), ("department_trend", {}),
                 ("department_trend", {"department": "Engineering", "from_": "2026-07-06"}),
                 ("attendance_list", {"page": 0}), ("attendance_list", {"status": ""}),
                 ("attendance_list", {"month": "invalid"}), ("attendance_list", {"limit": 51})]
        for endpoint, arguments in cases:
            with self.subTest(endpoint=endpoint, arguments=arguments):
                self.assert_error(422, lambda: self.code["explain_endpoint"](endpoint, **arguments))
        self.assertEqual(self.db.mock_calls, [])

    def test_unexpected_database_failures_are_not_fabricated_or_masked(self):
        self.db.command.side_effect = RuntimeError("unavailable")
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.code["explain_endpoint"]("attendance_list")
        self.db.employees.aggregate.side_effect = RuntimeError("unsupported server operator")
        with self.assertRaisesRegex(RuntimeError, "unsupported server operator"):
            self.code["department_summary"]("2026-07")


@unittest.skipUnless(HAS_HTTP, "FastAPI/Pydantic/httpx missing: analytics HTTP behavior unverified")
class AnalyticsHTTPTests(unittest.TestCase):
    def test_required_parameters_validation_and_from_alias(self):
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.testclient import TestClient
        code = load_helpers(include_analytics=True, query_factory=Query)
        app = FastAPI()
        for path, name in [
            ("/analytics/employees/{emp_code}/monthly", "employee_monthly"),
            ("/analytics/departments/summary", "department_summary"),
            ("/analytics/leaderboard/late", "late_leaderboard"),
            ("/analytics/departments/{department}/trend", "department_trend"),
            ("/admin/explain/{endpoint}", "explain_endpoint"),
        ]:
            app.get(path)(code[name])
        db = MagicMock()
        with patch.dict(code, db=db, HTTPException=HTTPException), TestClient(app) as client:
            for path in ("/analytics/employees/EMP0001/monthly", "/analytics/departments/summary",
                         "/analytics/leaderboard/late", "/analytics/departments/Engineering/trend",
                         "/admin/explain/employee_monthly"):
                response = client.get(path)
                self.assertEqual(response.status_code, 422)
                self.assertIsInstance(response.json()["detail"], list)
                self.assertEqual(db.mock_calls, [])
            response = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": "invalid"})
            self.assertEqual(response.status_code, 422)
            self.assertEqual(db.mock_calls, [])
            db.employees.aggregate.return_value = [{"date": "2026-07-06"}]
            response = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-06"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["items"], [{"date": "2026-07-06"}])
