# REVIEW.md



List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:



| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |

|---|---|---|---|---|

| 1 | `health` | Returns `{"status": "ok"}` unconditionally without checking if MongoDB is alive. Contract requires 503 when down. | Send request when MongoDB daemon is stopped; received 200 instead of 503. | Added `db.command("ping")` within a `try/except` block raising HTTPException(503) on failure. |

| 2 | `create\_employee` | Check-then-insert race condition (`find\_one` followed by `insert\_one`) without relying on a database unique index. | Concurrently sending duplicate POST `/employees` creates duplicate records or uncaught internal errors. | Created a unique index on `emp\_code` at startup and handled PyMongo's `DuplicateKeyError` to return HTTP 409. |

| 3 | `create\_employee` | Does not validate that `shift\_start != shift\_end` as mandated by contract. | Creating an employee with `shift\_start: "09:00"` and `shift\_end: "09:00"` succeeds with 201. | Added explicit check `if body.shift\_start == body.shift\_end: raise HTTPException(422)`. |

| 4 | `create\_employee` | Stores naive local datetime `datetime.now()` instead of UTC BSON date, and returns raw datetime instead of epoch milliseconds. | Contract tests checking `created\_at` in response received ISO string/float instead of epoch integer ms. | Store `datetime.now(timezone.utc)` truncated to whole seconds; convert to epoch ms in response. |

| 5 | `list\_employees` | Offset calculation bug: `skip = page \* page\_size` skips page 1 completely (page 1 drops first 20 records). | Calling `GET /employees?page=1\&page\_size=10` returns items 11–20 instead of 1–10. | Fixed offset calculation to `skip = (page - 1) \* page\_size`. |

| 6 | `list\_employees` | Total count bug: `total = db.employees.count\_documents({})` counts the entire collection, ignoring the `department` filter. | Filter by department with 2 employees; total returned total count of all 100 employees in DB. | Pass the query filter: `total = db.employees.count\_documents(q)`. |

| 7 | `list\_employees` | Missing required sorting by `emp\_code` ascending. | Returned documents in arbitrary natural MongoDB insertion order. | Added `.sort("emp\_code", ASCENDING)` to the cursor. |

| 8 | `punch\_in` | Uses check-then-insert (`find\_one` then `insert\_one`), failing under concurrency. | Parallel identical punch-in requests created duplicate records for the same employee and date. | Created a unique compound index on `("emp\_code", "date")`, catching `DuplicateKeyError` to return 409. |

| 9 | `punch\_in` | Ignores overnight shifts (Rule R1) and IST time zone when determining attendance date (`d = ts.date().isoformat()`). | Punching in at 05:30 AM IST for a 22:00–06:00 shift stamped today's date instead of previous day. | Implemented `determine\_attendance\_date` with IST offset and overnight shift logic. |

| 10 | `compute\_late\_minutes` | Treats punch\_in naive timestamps without IST timezone conversion; uses `>= 10` instead of strictly `> 10` (Rule R2). | Punch-in at 09:40:00 (10 min exactly) calculated 10 late minutes instead of 0. | Added IST conversion and checked `diff\_sec > 600` before calculating `math.floor(diff\_sec / 60)`. |

| 11 | `punch\_in` | Returns non-standard `id` field (`doc\["id"] = str(res.inserted\_id)`) which violates OpenAPI schema. | Response failed schema validation because `id` is not present in `AttendanceRecord`. | Removed `id` injection and serialized document strictly according to schema. |

| 12 | `list\_attendance` | Loads all matching records into Python memory (`list(db.attendance\_logs.find(q))`) and sorts in Python. | Crashes with OOM or severe latency on 100,000+ records. | Moved sorting and pagination directly to MongoDB cursor: `.sort(\[...]).skip().limit()`. |

| 13 | `list\_attendance` | Missing validation for `date\_from > date\_to`. | Querying `date\_from=2026-07-10\&date\_to=2026-07-01` returned 200 with empty items instead of 422. | Added check: `if date\_from and date\_to and date\_from > date\_to: raise HTTPException(422)`. |

| 14 | `compute\_work\_hours` | Uses standard Python `round()` (banker's rounding) instead of half-up rounding (Rule R4). | Values like 4.125 round to 4.12 instead of 4.13. | Implemented `ROUND\_HALF\_UP` using Python's `Decimal` module. |



\### Things examined and decided NOT to be defects

\- \*\*PyMongo synchronous driver vs Motor:\*\* The contract permits PyMongo or Motor. Using standard PyMongo with FastAPI sync def endpoints is fully supported by FastAPI threadpools and meets all concurrency and latency targets without async-event-loop overhead.

\- \*\*Absence of `\_id` field in API responses:\*\* The starter strips `\_id`. This is correct per `DATA\_MODEL.md` Section 2/3 ("Internal to MongoDB. Never accepted or returned by the API").

