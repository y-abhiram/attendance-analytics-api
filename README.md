# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6+ (tested with MongoDB 7).
All application code is in `app/main.py`. All contract endpoints are implemented;
indexes are created automatically at startup. Instants use epoch milliseconds at
the API boundary and UTC BSON dates in MongoDB.

## Run in Conda base

```bash
conda activate base
python --version                       # must be 3.11+
python -m pip install -r requirements.txt
cp .env.example .env                    # edit for your local MongoDB or Atlas
# Start a local server if needed (no Dockerfile is required):
docker run -d --name hrone-mongo -p 127.0.0.1:27017:27017 mongo:7
# Optional, only in the original kit: replaces both collections with samples.
# python sample_seed.py
python -m uvicorn app.main:app --port 8000 --reload
```

If `hrone-mongo` already exists, use `docker start hrone-mongo` instead of creating
it again. Open http://localhost:8000/docs for interactive requests. Real environment
variables override `.env`. A running MongoDB is required at startup.

## Verify

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q                     # real MongoDB needed for integration tests
python -m pytest -q -m 'not scale'       # omit the 100,000-record scale test
python -m pytest -q -m 'not integration' # time-rule and validation tests only
```

Integration tests use a fresh UUID database and delete that database afterwards.
Set `TEST_MONGO_URI` if it is not `mongodb://localhost:27017`. See `GUIDE.md` for a
walkthrough, `REVIEW.md` for starter defects, and `DECISIONS.md` for design choices.

Do not commit `.env`, secrets, virtual environments, Dockerfiles or data dumps.
The starter reference documents and sample data are local learning material;
submit the application, requirements, review, decisions and README, with tests
and the guide if desired. Do not publish the supplied problem statement.

Validation: 40 tests passed against local MongoDB 7, including 100,000 attendance
records and five indexed explain plans without collection scans. Local analytics
requests took about 2–206 ms; these timings depend on hardware and dataset shape.
