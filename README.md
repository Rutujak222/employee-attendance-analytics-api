# HROne Employee Attendance & Analytics API

FastAPI and MongoDB API for employees, attendance punches, manual corrections, analytics, and query explain output.
Application code is in `app/main.py`. `openapi.yaml` defines the API and business rules; `DATA_MODEL.md` defines MongoDB storage.
See `PROBLEM_STATEMENT.docx` for assignment instructions and `REVIEW.md` / `DECISIONS.md` for review findings and decisions.

## Requirements and installation

Use **Python 3.11+** and an available **MongoDB 6+** instance. The assignment grader uses MongoDB 7.0.
Run the commands below from the `candidate_kit` folder, which contains `requirements.txt` and `app/`.

Create and activate a virtual environment:

```powershell
# Windows PowerShell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

```bash
# Linux/macOS
python -m venv .venv
source .venv/bin/activate
```

With that environment active, install application dependencies:

```text
python -m pip install -r requirements.txt
```

## MongoDB configuration and startup

The API and sample loader read these environment variables:

| Variable | Purpose | Safe development example | Default when unset |
|---|---|---|---|
| `MONGO_URI` | MongoDB connection URI | `mongodb://localhost:27017` | `mongodb://localhost:27017` |
| `MONGO_DB` | Database containing `employees` and `attendance_logs` | `attendance_dev_disposable` | `attendance_db` |

Set them in the terminal used to run the API. The examples assume an existing local MongoDB instance and a disposable development database; replace them for your environment without publishing credentials.

```powershell
# Windows PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_dev_disposable"
```

```bash
# Linux/macOS
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_dev_disposable"
```

The application also supports a local `.env` through python-dotenv, but a file is not required. Existing environment variables take precedence.

Start the API with the virtual environment active:

```text
uvicorn app.main:app --port 8000
```

Startup creates indexes. `GET /health` returns `200` with `{"status":"ok"}` when MongoDB answers a ping, or `503` when the ping raises a MongoDB error. Index-creation failures prevent startup.

## Tests and verification status

Run the suite from the same project folder:

```text
python -B -m unittest discover -s tests -v
```

`httpx` is a testing-only dependency; it is intentionally installed separately from the application dependencies in `requirements.txt`. To enable the real Pydantic model tests and FastAPI HTTP tests, install the application dependencies above and then install `httpx` in the virtual environment:

```text
python -m pip install httpx
```

These HTTP tests use FastAPI's test client with mocked database operations; they do not require a running MongoDB instance and do not verify database integration.

The latest recorded run discovered **83 tests: 74 passed and 9 skipped** because dependencies were unavailable. Skipped tests have **not** passed. Real FastAPI/Pydantic validation and HTTP error responses remain unverified in that run.

Most runnable tests use extracted production definitions, mocks, or a limited expression evaluator. Real MongoDB aggregation results, index behavior and execution plans, concurrency, and performance remain unverified. Installing dependencies enables additional tests but does not establish MongoDB integration correctness.

## Optional sample data

**Warning: `sample_seed.py` deletes all existing documents in both `employees` and `attendance_logs` in the configured `MONGO_DB` before inserting sample data. Run it only against a disposable development database. Check both environment variables first.**

After installing dependencies and configuring that disposable database:

```text
python sample_seed.py
```

The nine sample documents illustrate data shapes. They are not sufficient to verify analytics, concurrency, or performance on the grader's larger dataset.

## Submission

Submit the required application, dependencies, review, decisions, and README in a public Git repository, following the assignment instructions. Do not publish `.env` files, credentials, virtual environments, data dumps, or a Dockerfile.
