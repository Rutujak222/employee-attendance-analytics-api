# Implementation review

`openapi.yaml` and `DATA_MODEL.md` are the contract. References below point to **current files and line numbers**, not original starter locations. Findings distinguish source inspection, isolated tests, and integration verification.

## Confirmed defects and fixes

| Current location | Affected behavior / evidence | Fix |
|---|---|---|
| `app/main.py:115`, `compute_late_minutes` | Flooring minutes before checking grace incorrectly treated 09:40:01 as on time for a 09:30 shift. Tests compare 09:40:00 -> 0 and 09:40:01 -> 10. | Compare elapsed seconds strictly against 600, then floor elapsed minutes. |
| `app/main.py:129`, `compute_work_hours` | Ordinary rounding returned 1.12 for 4,050 seconds (1.125 hours), instead of 1.13. | Decimal arithmetic with two-place `ROUND_HALF_UP`. |
| `app/main.py:140`, `compute_overtime` | Overtime below 30 minutes was counted. Regression cases cover 29:59 -> 0, 30:00 -> 30, and 31:00 -> 31. | Apply the minimum after flooring whole minutes. |
| `app/main.py:62`, `normalize_utc`; `:69`, `epoch_millis_to_utc` | Host-local conversions and mixed naive/aware datetimes were unsafe. Isolated tests compare simulated UTC/IST hosts and naive UTC inputs. | Explicit UTC conversion; treat naive BSON dates as UTC; truncate fractional seconds before calculations and writes. Server clocks request UTC explicitly. |
| `app/main.py:107`, `attendance_date`; `:115`, `:140` | Attendance dates and overnight anchors were incorrect. For 22:00–06:00, July 7 at 02:00 belongs to July 6; 06:40 punch-out gives 40 overtime minutes. | Use explicit IST, move the shift start/end anchors as required, and use the complete punch-out timestamp for hours. |
| `app/main.py:73`, `serialize_response` | Datetime responses did not follow the epoch-millisecond contract, including nested audit timestamps. | Recursively normalize/truncate datetime leaves and serialize integer milliseconds; preserve nulls, strings, and existing integers. |
| `app/main.py:479`, `punch_in`; `:628`, `list_attendance` | Attendance responses included an artificial identifier rather than only the natural identity. | Exclude internal `_id`; identify records by `(emp_code,date)`. |
| `app/main.py:399`, `EmployeeIn`; `:448`, `create_employee` | Documented field patterns, lengths, real calendar dates, and unequal shifts were not fully enforced. | Add exact field constraints and date validation; reject equal shifts before database access. Model/HTTP tests remain skipped. |
| `app/main.py:415`, `PunchInIn` | Timestamp bounds, strict integer types, explicit-null rejection, and presence-status choices were missing. | Declare strict bounded milliseconds and the three allowed statuses; preserve omission's clock default. HTTP rejection is not yet verified. |
| `app/main.py:479`, `punch_in` | A missing employee was dereferenced; isolated execution reproduced `TypeError` instead of the required 404. | Guard the employee lookup before attendance access. |
| `app/main.py:42`, `setup_indexes`; `:448`, `:479` | Check-then-insert alone could not enforce concurrent uniqueness; required indexes were absent. | Startup unique indexes plus duplicate-key-to-409 handling. Mocked tests cover conflicts and propagation of unrelated failures, not real races. |
| `app/main.py:467`, `list_employees` | Page 1 skipped 20 records; totals ignored department filters; sorting was absent. | Use `(page-1)*page_size`, count the same filter, and sort employee codes ascending. |
| `app/main.py:91`, `validate_pagination`; `:596`, `build_attendance_list` | Pagination bounds, date/status validation, and reversed-range rejection were absent. Supplied empty values were silently omitted. | Validate before querying; retain empty department/employee-code filters, but reject empty dates/statuses. |
| `app/main.py:628`, `list_attendance` | All matches were loaded and paginated in Python; equal dates lacked an employee-code tie-breaker. | MongoDB count/sort/skip/limit with date descending, employee code ascending; share the operation with explain. |
| `app/main.py:628`, `list_attendance` | Permitted legacy omissions produced incomplete responses. | Fill missing punch/work values with null, minutes with zero, `half_day` with false, and `history` with `[]`; preserve stored values. |
| `app/main.py:439`, `health` | Readiness always reported success without contacting MongoDB. | Ping MongoDB; translate driver failures to 503. Ping outcomes are tested with doubles. |

Additional development fixes: the timezone fallback now warns once per initialization (`app/main.py:33`); UTC+05:30 is retained when zone data is missing. Overtime now requires `shift_start`, with `TypeError` for omission and `ValueError` for explicit `None`, preventing a silent same-day assumption.

## Completed endpoints and source review

Punch-out (`app/main.py:512`) and regularization (`:538`) were added as required functionality. Conditional updates match the read attendance state; failed matches return 409. Corrections use `$push` for one changed-field-only audit entry; punch-out does not write history.

Analytics (`app/main.py:272`, `:299`, `:322`, `:345`) use stored derived fields. Explain (`:678`) shares production builders and requests `executionStats`. These are implementations, not claims of observed server correctness.

No source-level contract defect was found in the reviewed natural identity, preserved nulls/omitted correction fields, strict overnight shift-end boundary, seeded equal-shift handling, competition-rank cutoff, zero-log headcount strategy, or requested-range moving-window construction. Supplied derived fields are ignored under the contract's explicit global rule. Useful duplicate pre-checks remain, but are not the concurrency guarantee.

## Test evidence and gaps

Latest recorded command:

```text
python -B -m unittest discover -s tests -v
```

**83 tests run: 74 passed, 9 skipped (`OK (skipped=9)`).** Skipped tests did not pass.

Important coverage:

- `tests/test_calculation_helpers.py:74,91,108`: grace, half-up, and overtime boundaries.
- `tests/test_timestamp_normalization.py:14,33,67`: epoch conversion, host independence, and UTC clocks.
- `tests/test_attendance_dates.py:32,79,93,99`: overnight dates, adjacent shift-end seconds, overtime, hours, and natural identity.
- `tests/test_response_serialization.py:15,43,76`: nested timestamps, null preservation, integer idempotence, and listing serialization.
- `tests/test_listing_and_health.py:35,67,81,109,133,162,167`: query construction, pagination, invalid filters, legacy defaults, and readiness.
- `tests/test_attendance_writes.py:79,86,120,194,223`: rounded half-days, 24-hour boundaries, audit changes, no-ops, and unmatched-update conflicts.
- `tests/test_database_integrity.py:26,71,83`: startup index requests, duplicate-key conflicts, and unrelated failures.
- `tests/test_analytics_and_explain.py:95,103,125,159,181,216`: working-day/metric expressions, rounding, ranking stages, trend generation/window structure, and identical explain commands.

Remaining gaps include real selection among multiple punch records, fractional seeded timestamps at eligibility boundaries, multi-employee record-weighted averages, rank gaps, entirely empty trends, early/null-only moving windows, and concurrent requests. Current mocks and the limited evaluator do not execute complete MongoDB pipelines.

## Verification limits

The nine dependency-gated tests cover real model/HTTP paths; they remain unverified because dependencies are unavailable. Runnable tests extract actual production definitions, use exception/database doubles, or evaluate selected expressions in Python. They do not establish full application startup or HTTP error encoding.

No real MongoDB aggregation execution, concurrency test, BSON serialization against the server, index execution plan, or performance result has been verified. Explain command construction is tested; actual `IXSCAN`/no-`COLLSCAN` evidence is still required. **This report is not a full integration sign-off.**
