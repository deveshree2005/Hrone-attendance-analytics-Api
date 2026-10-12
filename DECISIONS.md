# DECISIONS.md

**1. Indexes.** I created six indexes. A unique index on `employees.emp_code` prevents duplicate employee codes. A department/joined-date index supports department and headcount queries. A unique compound index on attendance employee code and date prevents duplicate daily attendance. The date/employee index supports attendance sorting, the employee/date/status index supports attendance filters, and the date/late-minutes/employee index helps late-arrival queries. I would consider a `punch_in` index for punch-out lookups, but measure its benefit before adding it.

**2. Punch-in race.** Two requests can both check the employee and attempt insertion. The unique attendance index permits only one record for the same employee and date. The other insertion raises `DuplicateKeyError`, which the API converts to HTTP 409.

**3. Ties.** The leaderboard uses MongoDB `$rank` and includes every employee whose rank is at most `limit`. Employees tied at the cutoff are all returned with the same competition rank.

**4. Headcount.** The department summary starts from employees and looks up their attendance logs. Using `$unwind` with `preserveNullAndEmptyArrays: True` keeps employees who have no logs, so they are still included in headcount.

**5. Scaling.** For 100 times more data, I would examine query execution plans and index usage, then optimize aggregation pipelines and add indexes only where measurements justify them.