# Implementation walkthrough

The application is complete. Use this guide to work through the implementation,
run examples and explain each part yourself. Review and rewrite the decisions in
your own words once you understand them.

## 1. Start with the environment

Your Conda base has Python 3.12. Run `conda activate base`, then install
`requirements.txt` using `python -m pip`. Set `MONGO_URI` and `MONGO_DB` in your shell
or copy `.env.example` to `.env`. Keep `.env` private. MongoDB 7 works locally in the
`hrone-mongo` Docker container; no Dockerfile is needed.

The server starts with `python -m uvicorn app.main:app --port 8000 --reload`.
Visit `/health`, then `/docs`. The lifespan function creates indexes before the
app accepts requests. This prevents duplicate employee codes and attendance days.

## 2. Read the models and time helpers

`EmployeeIn`, `PunchInIn`, `PunchOutIn` and `RegularizeIn` validate inputs before
endpoint code executes. Unknown derived fields are ignored. Timestamps are strict
integers in the specified range; null, strings, floats and epoch seconds fail.
Calendar validation checks real dates, not just the string shape.

`instant` turns epoch milliseconds into an aware UTC datetime and drops fractional
seconds. `attendance_day` converts that instant to IST and handles early-morning
overnight punches. `shift_bounds` supplies actual shift-start/end instants.

Try explaining why an overnight punch at 05:59 belongs to yesterday but one at
06:00 belongs to today for a 22:00–06:00 shift. Then inspect the boundary tests.

`derived_values` applies the business rules in one place. Late grace compares
seconds before flooring minutes. Work hours use Decimal half-up rounding; half-day
uses the rounded hours. Overtime starts at 30 full minutes. `json_value` converts
nested BSON datetimes back to epoch milliseconds, including audit history.

## 3. Follow the write endpoints

Employee creation and punch-in attempt inserts directly. The database's unique
index decides which concurrent request succeeds; duplicate-key exceptions become
409. A Python existence check alone cannot enforce uniqueness.

Punch-out finds the latest punch-in at or before the requested instant, rather
than assuming the punch-out calendar date equals the attendance date. It validates
the duration, calculates fields and applies an atomic compare-and-set update.

Correction builds a final record from the stored state and supplied fields. It
checks status/time compatibility, attendance day and duration; recomputes derived
fields; and collects only fields whose values changed. A single MongoDB update
sets the fields and pushes one history entry.

`snapshot_filter` matches the original field values and history. If another writer
has changed that snapshot, the update returns no record and the endpoint returns
409. This prevents stale writes and lost audit entries without transactions or
extra schema fields. A subsequent request may retry after rereading the record.

## 4. Try a complete day in Swagger

Create this employee with `POST /employees`:

```json
{
  "emp_code": "EMP0100",
  "name": "Demo Employee",
  "email": "demo@example.com",
  "department": "Engineering",
  "joined_on": "2026-01-01"
}
```

Generate the timestamps in your base terminal:

```python
from datetime import datetime
from zoneinfo import ZoneInfo
for value in ["2026-07-06T10:05:00", "2026-07-06T19:10:00", "2026-07-06T09:28:00"]:
    print(value, int(datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp() * 1000))
```

Use the first timestamp as `punched_at` for `/attendance/punch-in`, then the second
for `/attendance/punch-out`, with `emp_code: EMP0100`. Expected: late 35 minutes,
overtime 40 minutes, work_hours 9.08, half_day false, empty history.

Patch `/attendance/EMP0100/2026-07-06` with the third timestamp as `punch_in`,
`reason: biometric glitch`, and `regularized_by: hr.admin`. Expected: late becomes
0, work_hours becomes 9.70, and history lists the changed punch-in, late minutes
and work hours. Original punch events do not themselves create audit entries.

## 5. Understand the analytics builders

Every builder produces a MongoDB pipeline. Python constructs expressions; MongoDB
processes the attendance documents. Analytics read stored derived fields.

- `monthly_pipeline`: match employee/month, then group presence weights, leave,
  late and overtime. Working days use calendar arithmetic from employment start.
- `summary_pipeline`: start with employees who joined by month end, look up grouped
  logs, then group by department. Keep employees with empty lookups. Sum hours and
  contributing-record counts separately so the mean is weighted by records.
- `leaderboard_pipeline`: group late totals, join employees, filter department,
  apply `$rank`, then filter rank and sort. With totals 60, 30, 30, 10, ranks are
  1, 2, 2, 4. A limit of 2 returns three rows.
- `trend_pipeline`: group a department's employee codes and joining dates, generate
  the requested calendar with `$range`/`$map`, unwind one row per day, and look up
  that day's grouped logs. This also works for a range with no logs at all.
  Weekends have null attendance rates; empty weekdays with headcount have zero.
  `$setWindowFields` averages non-null rates in the current and preceding six rows.

MongoDB's `$round` uses ties-to-even. `round_half_up` instead uses Decimal arithmetic
and `floor(value * scale + 0.5) / scale` for nonnegative metrics.
Reference: https://www.mongodb.com/docs/manual/reference/operator/aggregation/round/
Ranking reference: https://www.mongodb.com/docs/manual/reference/operator/aggregation/rank/

## 6. Check database plans

For example, request:

```text
/admin/explain/attendance_list?page=1&page_size=20
/admin/explain/employee_monthly?emp_code=EMP0100&month=2026-07
/admin/explain/department_summary?month=2026-07
/admin/explain/late_leaderboard?month=2026-07&limit=2
/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-10
```

The explain endpoint uses the same builders, filters, sort, pagination and hints
as the real endpoint, with `executionStats` verbosity. Look for IXSCAN, inspect
examined keys/documents, and check that COLLSCAN is absent. A hint ensures an index
is used, but does not by itself prove that a query is efficient; inspect the counts.

## 7. Test and prepare submission

Run `python -m pytest -q` with MongoDB running. Tests cover grace boundaries,
overnight shifts, strict input types, concurrent writes, correction history,
legacy fields, joins, gaps, weekends, mid-month hires, ranks and explain plans.

Before submitting, run from a clean clone, read every function, and practice
changing a rule. For example, find the two places needed to change half-day hours
and its test. Read the review table and verify its examples yourself.

Only include intended submission files and tests in the public repository. Keep
credentials and the original problem statement out. Submit the repository link
by 15 October 2026 at 11:59 PM IST, keep it public until 31 October, and be ready
to walk through the implementation live. No repository has been published or
form submitted by this implementation work.

Once you create a Git repository, stage submission files explicitly:

```bash
git add app/main.py app/__init__.py requirements.txt requirements-dev.txt README.md REVIEW.md DECISIONS.md GUIDE.md .env.example .gitignore pytest.ini tests/
git status --short
```

The ignore file keeps the original kit documents and samples local. Only create
and publish the public repository after reviewing the code and staged files.
