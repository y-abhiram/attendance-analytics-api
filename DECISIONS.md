# Design decisions

1. **Indexes.** Unique `employee_code` and `employee_date` indexes enforce business identities. `department_code` serves department filtering and employee ordering; `joined_department` serves period headcount. `date_employee` supports global attendance ordering and month scans. `status_date_employee` supports status filtering with that ordering. `employee_punch` finds the latest eligible punch across midnight. I rejected indexing `half_day` alone: it has little selectivity and no endpoint filters on it. Queries and their explains use the same hints.

2. **Punch-in race.** Both requests validate the employee and calculate the attendance day. Both attempt insertion. MongoDB's unique employee/date index allows one insert; the other raises `DuplicateKeyError`, mapped to 409. There is no separate existence check.

3. **Ties.** `$rank` runs before the rank cutoff. Everyone with rank at most `limit` is returned, including ties. Employee code breaks display-order ties without changing rank.

4. **Headcount.** Department summary starts from eligible employees, then looks up each employee's aggregated logs. Empty lookup results contribute zero metrics while retaining one employee in headcount.

5. **At 100x.** I would measure query plans and latency, then introduce incrementally maintained daily summaries for department analytics. The trend's employee arrays and per-day lookups would need particular attention. Raw attendance and correction history would remain authoritative.
