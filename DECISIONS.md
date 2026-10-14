# Implementation decisions

1. **Indexes.** Unique `employees(emp_code)` and `attendance_logs(emp_code,date)` indexes enforce identity. Employee indexes on `(department,joined_on,emp_code)` and `(joined_on,department,emp_code)` support headcount queries. Attendance `(date DESC,emp_code ASC)` supports date ranges and ordering. Production hints are shared with explain. No rejected index is documented; execution plans remain unverified.

2. **Concurrent writes.** Both punch-ins may pass the duplicate pre-check. The unique attendance index allows one insert: the winner returns 201; duplicate-key errors become 409. Punch-out and corrections conditionally match the previously read attendance state, including history; unmatched updates return 409. Corrections append history with `$push`.

3. **Ties.** MongoDB `$rank` gives competition ranks, such as 1, 2, 2, 4. Applying `rank <= limit` afterward keeps every qualifying tie, so results may exceed the limit. Equal totals are ordered by `emp_code`.

4. **Headcount.** The summary starts with employees whose `joined_on` is no later than month-end. Its lookup preserves employees without logs. Grouping counts each employee once; missing attendance metrics contribute zero.

5. **Scaling.** At 100x the data, I would first measure explain plans and profile repeated lookups and growing history snapshots before changing indexes or introducing precomputed summaries. These are proposals: production performance, real MongoDB aggregation/concurrency, and HTTP validation remain unverified.
