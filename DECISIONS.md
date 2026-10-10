# DECISIONS.md

## 1. Indexes

I created five indexes at startup (all idempotent via `create_index`):

| Index | Collection | Fields | Purpose |
|---|---|---|---|
| `employees_emp_code_unique` | `employees` | `emp_code` ASC, unique | Primary lookup key for every employee read; unique constraint enforces concurrent-create safety at the DB level. |
| `employees_dept_empcode` | `employees` | `department` ASC, `emp_code` ASC | Covers the `GET /employees?department=…` filter and the ascending `emp_code` sort in one IXSCAN. |
| `employees_joined_on` | `employees` | `joined_on` ASC | Used by the headcount aggregations (R9) that filter `joined_on <= last_day`. |
| `attendance_emp_date_unique` | `attendance_logs` | `(emp_code, date)` ASC, unique | The natural key for a record; prevents duplicate punch-ins atomically (no check-then-insert race). Also covers single-record lookups and the regularize PATCH. |
| `attendance_date_empcode` | `attendance_logs` | `date` DESC, `emp_code` ASC | Matches the default sort of `GET /attendance` exactly, so the list query is a pure IXSCAN with no in-memory sort. |
| `attendance_date_asc` | `attendance_logs` | `date` ASC, `emp_code` ASC | Covers monthly analytics `$match` on `{date: {$gte, $lte}}` and the department trend query. |

**Index I considered and rejected:** a sparse index on `late_minutes` for the leaderboard. Because the leaderboard already filters `late_minutes > 0` inside an aggregation that also filters by date range, the existing `attendance_date_asc` index is the right entry point; adding a second index on `late_minutes` alone would not help and would slow down writes.

## 2. Punch-in race

Two identical `POST /attendance/punch-in` requests arrive at exactly the same instant for the same employee on the same attendance date.

Both requests pass Pydantic validation, look up the employee, compute the attendance date and `late_minutes`, and attempt `insert_one`. Because the `(emp_code, date)` unique index exists at the MongoDB level, only one insert can succeed — MongoDB serialises the two writes internally and rejects the second with a `DuplicateKeyError`. My code catches that error and returns 409. The first caller gets 201; the second gets 409. There is no check-then-insert window where both could succeed.

## 3. Ties in the leaderboard

The pipeline uses MongoDB's `$setWindowFields` with `$rank`, which implements standard competition ranking (1, 2, 2, 4). After ranking, I keep every row whose `rank <= limit`. If two employees share rank 10 and `limit=10`, both are included — the response can have more than `limit` rows, which is exactly what the spec requires. Rows are ordered by `total_late_minutes` descending, then `emp_code` ascending, so the output is deterministic even when totals are equal.

## 4. Headcount with zero-log employees

The department summary first aggregates the `employees` collection directly (filtered by `joined_on <= last_day`, R9) to produce a headcount and list of `emp_codes` per department. It then does a separate aggregation on `attendance_logs` joined back to `employees` via `$lookup` to collect the log statistics. The two results are merged in Python. Because headcount comes from the employees collection — not from logs — an employee who joined before or during the month but never swiped in still contributes 1 to `headcount`.

## 5. One change for 100x data

I would move the present-days Mon-Fri filtering out of Python and into the aggregation pipeline using `$addFields` with `$dayOfWeek` on a `$dateFromString` expression (converting the `date` string to a BSON date at midnight IST, then checking `$dayOfWeek` is between 2 and 6). This eliminates the `"records": {"$push": …}` accumulator that currently pulls every record document back to the application layer. At 10 million records that push would transfer gigabytes of data; keeping it all inside a single aggregation stage would keep memory and network use constant regardless of data volume.
