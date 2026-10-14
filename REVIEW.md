# Starter review

Locations below refer to the original starter's functions, before replacement.

| # | Where | What was wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start was built in the punch timestamp's timezone, not IST. | A UTC punch at 04:35 for a 09:30 IST shift should be 35 minutes late. | Build shift bounds on the attendance day in IST; compare aware UTC instants. |
| 2 | `compute_late_minutes` | Flooring before checking grace lost seconds at the boundary. | 09:40:01 incorrectly returned 0. | Compare elapsed seconds against 600 first, then floor minutes. |
| 3 | `compute_late_minutes`, `punch_in` | Overnight attendance dates and shift starts were not handled. | A 05:00 punch for 22:00–06:00 belongs to the previous day. | Derive the attendance day with R1 and build bounds from it. |
| 4 | `compute_work_hours` | Float `round` did not guarantee decimal half-up. | 18 seconds = 0.005 hours must round to 0.01. | Divide and quantize using `Decimal` and `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Overtime below 30 minutes was counted. | Punch-out 18:59 for an 18:30 end should return 0. | Floor elapsed minutes; count only values >=30. |
| 6 | `compute_overtime` | Overnight end was placed on the starting date. | 06:40 next morning for 22:00–06:00 should be 40 minutes overtime. | Put an overnight shift's end on the next calendar day. |
| 7 | Timestamp handling | `fromtimestamp` and `now` created host-local naive times. | Run on a UTC host; attendance days and late values change. | Use aware UTC datetimes and `tz_aware=True` in PyMongo. |
| 8 | `PunchInIn` | Integers were coerced; seconds-looking and out-of-range timestamps were accepted. | Send a numeric string, float, boolean or epoch seconds. | Strict bounded integers; explicit null is invalid, omission uses now. |
| 9 | `punch_in`, helpers | Milliseconds were not truncated before storage/calculation. | 09:40:00.900 must count as 09:40:00. | Truncate every supplied punch instant to whole seconds. |
| 10 | `EmployeeIn` | Code, email, lengths, calendar dates and shift times were unvalidated. | Send `EMPx`, a malformed email or February 30. | Pydantic constraints plus real calendar parsing. |
| 11 | `EmployeeIn` | Equal shift times were accepted. | Create an employee with 09:30–09:30. | Model validator rejects equal times with 422. |
| 12 | `PunchInIn` | Any status string was accepted. | Punch in with `LEAVE` or `invalid`. | Restrict punch-in to PRESENT, WFH and ON_DUTY. |
| 13 | `health` | Returned ready even if MongoDB was down. | Stop MongoDB and request `/health`. | Ping MongoDB; return 503 on database failure. |
| 14 | Startup | No indexes were created. | Seed only default indexes; examine plans and send duplicate punches. | Idempotently create unique and query indexes in lifespan startup. |
| 15 | `create_employee` | Check-then-insert allowed concurrent duplicates. | Submit identical creates in parallel. | Unique employee-code index; catch duplicate-key errors as 409. |
| 16 | `create_employee` | `created_at` used naive local time. | Compare its value on hosts in different timezones. | Set a server-generated UTC instant. |
| 17 | Employee/attendance responses | Datetimes were returned as ISO strings, including nested audit times. | Inspect `created_at`, `punch_in` and history changes. | Recursively serialize BSON dates as integer epoch milliseconds. |
| 18 | `list_employees` | Skip was `page * page_size`, losing the first page. | Page 1 of a one-employee database was empty. | Use `(page - 1) * page_size`. |
| 19 | `list_employees` | Total ignored the department filter. | Filter one department in a multi-department database. | Count using the same filter and index. |
| 20 | `list_employees` | No deterministic employee-code sort. | Insert employee codes out of order. | Sort in MongoDB by `emp_code` ascending. |
| 21 | Both list endpoints | Invalid pagination was accepted. | Send page 0 or page_size 101. | Validate page >=1 and page_size 1–100. |
| 22 | `punch_in` | Missing employee was dereferenced, causing 500. | Punch in with an unknown code. | Resolve employee first; return 404. |
| 23 | `punch_in` | Check-then-insert allowed duplicate employee/day records. | Run simultaneous punches for one attendance day. | Unique employee/date index and duplicate-key handling. |
| 24 | Attendance responses | Exposed MongoDB identifiers as `id`. | Inspect punch-in and attendance-list responses. | Omit internal identifiers; address records by employee/date. |
| 25 | `list_attendance` | Loaded and sorted every matching document in Python. | List a page from 100,000 records. | Indexed database sort, skip, limit and filtered count. |
| 26 | `list_attendance` | Ties on date lacked employee-code ordering. | Two employees have records on the same date. | Sort by date descending, then employee code ascending. |
| 27 | `list_attendance` | Date strings, reversed ranges and status filters were not validated. | Send an impossible date, reversed range or unknown status. | Validate calendar strings, enums and range order; return 422. |
| 28 | Legacy record output | Missing fields remained missing. | List a seed with no `history` or `half_day`. | Supply empty history, false half_day and zero missing minute totals. |
| 29 | Department filters | Truthiness ignored an explicit empty department filter. | Query `department=`; it should not mean every department. | Distinguish omitted (`None`) from an exact string match. |

The TODO endpoints were missing features, rather than defects in existing functions. They are now implemented, including duration validation, derived values, atomic correction history, aggregation analytics and shared explain builders.

Examined and kept: employee codes are client-supplied business identifiers, not ObjectIds; dates and shift times intentionally stay strings in MongoDB; `.env` loading is appropriate and real environment variables retain priority; ignoring extra body fields is correct for read-only derived fields. The starter's `minutes > 10` expresses the intended grace rule, but its earlier integer conversion made the implementation incorrect.
