# DECISIONS.md

1. **Indexes.** I create a unique index on `employees.emp_code` and a unique compound index on `attendance_logs.emp_code` plus `date`. The second index is the database-level guarantee that two simultaneous punch-ins cannot create duplicate daily records. I also index department/join date for headcount queries, date/employee for month and list queries, and status/date for filtered attendance. I would avoid adding an index for every field because every index costs storage and slows writes.

2. **Punch-in race.** Both requests may initially see no record, but only one insert can satisfy the unique compound index. MongoDB accepts one; the other raises `DuplicateKeyError`, which the API returns as 409.

3. **Ties.** I use MongoDB `$rank`, so equal totals share a rank and the next rank is skipped. I filter on rank after ranking, meaning ties at the cutoff are all returned. Rows are then ordered by total minutes and employee code.

4. **Headcount.** The department summary starts from employees and looks up attendance logs. This keeps eligible employees with no logs in the group. `joined_on` is checked against the month end so future joiners do not count.

5. **100x data.** I would load-test aggregation plans and add measured indexes, then consider pre-aggregated daily summaries if response-time targets require them.
