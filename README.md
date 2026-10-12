# REVIEW.md

## Defects and verification

The original starter-code version was not available for a direct comparison during this review. Therefore, I have not attributed unverified defects to the starter code.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | Employee listing: `list_employees` | Verified that pagination starts at page 1, uses the same filters for the count and results, and calculates the correct offset. No defect identified in the current implementation. | Request page 1 and page 2, with and without a department filter; check totals and records. | No further change needed based on this inspection. |
| 2 | Attendance listing: `list_attendance` | Verified date-range validation, status filtering, sorting, and pagination in the current implementation. No defect identified in the inspected code. | Try reversed date ranges, valid filters, and multiple pages. | No further change needed based on this inspection. |

## Inspected but not considered defects

The punch-in handler catches `DuplicateKeyError`; its duplicate-prevention guarantee depends on the unique `(emp_code, date)` index. Punch-out uses a conditional update to detect concurrent changes. The leaderboard uses MongoDB `$rank` and includes employees tied at the cutoff. The department summary starts from employees and preserves records with no attendance logs. These behaviors appear consistent with the intended requirements in the inspected code, but should also be tested against the complete assignment contract.
