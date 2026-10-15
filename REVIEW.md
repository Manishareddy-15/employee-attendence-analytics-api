# REVIEW.md

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Replaces the punch-in's date but ignores the shift's actual attendance date/time-zone rules. | Test an overnight shift or a punch near midnight. | Calculate against the IST shift start for the resolved attendance date. |
| 2 | `compute_late_minutes` | Naive subtraction and truncation can produce inconsistent results around seconds/time zones. | Send timestamps with non-zero milliseconds or UTC values. | Normalize to UTC and truncate to whole seconds at the boundary. |
| 3 | `compute_work_hours` | Python `round` uses half-even rounding, not required half-up rounding. | Use a duration whose hour value is exactly at a half-cent boundary. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Builds shift end on the same calendar date, so overnight shifts are wrong. | Punch out at 06:40 for a 22:00–06:00 shift. | Advance the shift end to the next day when the shift is overnight. |
| 5 | `POST /employees` | Duplicate check followed by insert is race-prone; two requests can both pass the check. | Send parallel creates with the same employee code. | Enforce unique index and map `DuplicateKeyError` to 409. |
| 6 | `GET /employees` | Skip is `page * page_size`, skipping the first page; total ignores the department filter; results are not sorted. | Page 1 misses initial employees and filtered total is too high. | Use `(page-1)*page_size`, count the same filter, sort by code. |
| 7 | `POST /attendance/punch-in` | Does not verify employee exists before dereferencing it; missing employee causes 500. | Punch in with unknown code. | Return 404 before using employee fields. |
| 8 | `POST /attendance/punch-in` | Uses local machine time and seconds rather than epoch milliseconds/UTC; fails to truncate milliseconds. | Run on a non-IST host or submit timestamp with milliseconds. | Convert input milliseconds to UTC, truncate to seconds, and derive the IST attendance date. |
| 9 | `POST /attendance/punch-in` | Status is not constrained to presence statuses; no joined-date validation. | Submit `LEAVE` or punch before joining. | Validate status and reject a punch before `joined_on`. |
| 10 | `POST /attendance/punch-in` | Check-then-insert cannot guarantee one record under concurrency. | Send simultaneous punch-ins for one employee/date. | Unique compound index on `(emp_code,date)` and handle duplicate-key conflict. |
| 11 | `GET /attendance` | Fetches all matching documents and sorts/pages in Python; unbounded memory/time. | Query a large dataset. | Count filtered query and sort/skip/limit in MongoDB with indexes. |
| 12 | `GET /attendance` | Does not exclude `_id` or convert timestamps/history to API milliseconds. | Compare response to the contract. | Serialize only contract fields and convert all instants, including nested history. |
| 13 | `GET /health` | Returns success without checking MongoDB connectivity. | Stop MongoDB and call health. | Ping MongoDB and return 503 when unavailable. |
| 14 | All endpoints | Missing startup indexes and input constraints permit duplicate keys and unbounded pagination. | Inspect indexes or request page_size 1000. | Create idempotent indexes at startup and validate pagination limits. |
| 15 | Analytics TODOs | Required endpoints and aggregation-based analytics are missing. | Call any listed analytics endpoint. | Implement monthly, department, leaderboard, trend, and explain endpoints. |

Reviewed and not treated as defects: using `emp_code` as the business identifier is intentional; `_id` is MongoDB-internal. A missing `history` array and `half_day` field are valid legacy records and must default to `[]` and `false`.
