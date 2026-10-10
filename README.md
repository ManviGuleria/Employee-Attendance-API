# Employee Attendance & Analytics API

A FastAPI + MongoDB REST API for tracking employee punch-ins, punch-outs, manual corrections, and attendance analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0 or newer (local Docker or MongoDB Atlas free tier both work)

## Local setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv && source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env
# Edit .env and set:
#   MONGO_URI=mongodb://localhost:27017
#   MONGO_DB=attendance_db

# 4. (Optional) Start a local MongoDB with Docker
docker run -d -p 27017:27017 mongo:7

# 5. (Optional) Seed the nine sample documents
python sample_seed.py

# 6. Start the server
uvicorn app.main:app --port 8000 --reload
```

The API will be available at `http://localhost:8000`.  
Interactive docs: `http://localhost:8000/docs`

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

## Project structure

```
app/
  __init__.py
  main.py          ← all application code lives here
sample_data/
  employees.json
  attendance_logs.json
sample_seed.py     ← loads sample_data/ into MongoDB
requirements.txt
openapi.yaml       ← full API contract and business rules R1-R10
DATA_MODEL.md      ← MongoDB collection schemas and sample documents
REVIEW.md          ← bugs found and fixed in the starter code
DECISIONS.md       ← design decisions and trade-offs
```

## Key design points

- **All code is in `app/main.py`** as required.
- Indexes are created idempotently at startup; no manual setup needed.
- Instants are stored as BSON UTC datetimes and serialised as epoch milliseconds at the API boundary.
- Calendar dates and shift times are plain strings (`YYYY-MM-DD`, `HH:MM`) as specified.
- Punch-in duplicate prevention is handled atomically by the unique `(emp_code, date)` MongoDB index — no check-then-insert race.
- Rounding uses `ROUND_HALF_UP` via Python's `Decimal` module (R4, R8).
- Overnight shifts (e.g. 22:00–06:00) are handled correctly throughout: attendance date, late minutes, and overtime all account for the shift spanning midnight.

## What is implemented

- `GET  /health` — MongoDB ping, 503 if unavailable
- `POST /employees` — create employee (unique index, 409 on duplicate)
- `GET  /employees` — list with department filter, sorted by emp_code, paginated
- `POST /attendance/punch-in` — R1 date, R2 late minutes, concurrent-safe
- `POST /attendance/punch-out` — R3 overtime, R4 work hours, R5 half day, concurrent-safe
- `GET  /attendance` — filtered list, server-side sort + pagination
- `PATCH /attendance/{emp_code}/{date}` — regularize with full audit trail
- `GET  /analytics/employees/{emp_code}/monthly` — R5–R8 monthly summary
- `GET  /analytics/departments/summary` — R8–R9 department aggregation
- `GET  /analytics/leaderboard/late` — standard competition ranking with ties
- `GET  /analytics/departments/{department}/trend` — daily trend with 7-day moving average
- `GET  /admin/explain/{endpoint}` — MongoDB explain with executionStats

## Notes

- The `.env` file is not committed. Copy `.env.example` and fill in your values before running.
- No Dockerfile is included as per submission requirements.
