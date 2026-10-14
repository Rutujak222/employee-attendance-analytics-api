"""Exercise the real arithmetic helpers without app dependencies or MongoDB.

Only standard-library imports, IST, and selected production definitions are loaded
from main.py. These isolated checks do not verify application startup or HTTP.
"""
import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest


def load_helpers(include_write_endpoints=False, include_list_endpoints=False, include_startup=False,
                 include_analytics=False, query_factory=None):
    source = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = {
        "normalize_utc", "epoch_millis_to_utc", "compute_late_minutes",
        "compute_work_hours", "compute_overtime",
        "serialize_response",
        "attendance_date",
        "query_validation_error", "validate_pagination", "validate_query_date",
        "attendance_validation_error", "attendance_document", "attendance_snapshot_filter",
        "validate_punch_interval", "closed_attendance_values",
        "build_attendance_list",
    }
    if include_write_endpoints:
        names.update({"create_employee", "punch_in", "punch_out", "regularize_attendance"})
    if include_list_endpoints:
        names.update({"list_employees", "list_attendance", "health"})
    if include_startup:
        names.update({"setup_indexes", "lifespan"})
    if include_analytics:
        names.update({"month_bounds", "trend_bounds", "mongo_calendar_date", "mongo_working_day",
                      "mongo_presence", "mongo_half_up", "monthly_metrics_group", "monthly_logs_lookup",
                      "aggregate_operation", "build_employee_monthly", "build_department_summary",
                      "build_late_leaderboard", "build_department_trend", "analytics_json", "run_analytics",
                      "employee_monthly", "department_summary", "late_leaderboard", "department_trend",
                      "explain_endpoint"})
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Import) and all(alias.name in {"logging", "json"} for alias in node.names):
            nodes.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module in {"calendar", "contextlib", "datetime", "decimal", "typing", "zoneinfo"}:
            nodes.append(node)
        elif isinstance(node, ast.Try) and any(
            isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "IST"
                for target in item.targets
            ) for item in node.body
        ):
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "IST"
            for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            if node.name != "lifespan":
                node.decorator_list = []
            nodes.append(node)
    # Annotation placeholders only; endpoint tests pass simple request objects.
    namespace = {"__name__": "app.main", "EmployeeIn": object, "PunchInIn": object,
                 "PunchOutIn": object, "RegularizeIn": object,
                 "Query": query_factory or (lambda **kwargs: None)}  # Metadata only unless supplied by HTTP tests.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


HELPERS = load_helpers()
IST = timezone(timedelta(hours=5, minutes=30))


class CalculationHelpersTests(unittest.TestCase):
    def test_late_grace_and_floor_boundaries(self):
        start = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
        cases = [
            (-60, 0), (0, 0), (599, 0),
            (600, 0),     # 09:40:00: exactly at the grace boundary.
            (601, 10),    # 09:40:01: late; count from 09:30, not 09:40.
            (659, 10), (660, 11),
            (2759, 45),   # 10:15:59: floor 45 minutes 59 seconds to 45.
        ]
        for seconds, expected in cases:
            with self.subTest(punch_time=(start + timedelta(seconds=seconds)).isoformat()):
                self.assertEqual(
                    HELPERS["compute_late_minutes"](
                        start + timedelta(seconds=seconds), "09:30"
                    ), expected
                )

    def test_work_hours_round_half_up(self):
        start = datetime(2026, 7, 6, 9, 30, tzinfo=IST)
        cases = [
            (0, 0.0), (3600, 1.0),
            (4049, 1.12),  # One second below the 1.125-hour boundary.
            (4050, 1.13),  # Exact tie must round half-up, not to 1.12.
            (4051, 1.13),  # One second above the boundary.
            (16181, 4.49), (16182, 4.50),
        ]
        for seconds, expected in cases:
            with self.subTest(elapsed_seconds=seconds):
                self.assertEqual(
                    HELPERS["compute_work_hours"](
                        start, start + timedelta(seconds=seconds)
                    ), expected
                )

    def test_day_shift_overtime_minimum_and_floor(self):
        end = datetime(2026, 7, 6, 18, 30, tzinfo=IST)
        cases = [
            (-60, 0), (0, 0), (300, 0),
            (1799, 0),    # 29:59 after shift end: below the minimum.
            (1800, 30),   # 30:00: exactly meets the minimum.
            (1859, 30),   # 30:59: count only whole minutes.
            (1860, 31),   # 31:00: above the threshold.
        ]
        for seconds, expected in cases:
            with self.subTest(seconds_after_end=seconds):
                self.assertEqual(
                    HELPERS["compute_overtime"](
                        end + timedelta(seconds=seconds), "18:30", "2026-07-06",
                        shift_start="09:30",
                    ), expected
                )


if __name__ == "__main__":
    unittest.main()
