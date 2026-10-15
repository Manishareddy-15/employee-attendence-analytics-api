# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo, and MongoDB 6.0+.

1. Set `MONGO_URI` and `MONGO_DB` in your environment.
2. Install dependencies with `pip install -r requirements.txt`.
3. From the repository root run `uvicorn app.main:app --port 8000`.

Indexes are created idempotently at startup. No `.env`, secrets, or Dockerfile should be committed. Read `DATA_MODEL.md` and `openapi.yaml` for contract details.

**Before submitting:** run against MongoDB 6.0+ and test all routes, especially concurrency, aggregation edge cases, and explain plans. This implementation has been syntax-checked but has not been integration-tested against a live MongoDB instance in this environment.
