# Railway deployment

The service builds from the `Dockerfile` (the `Procfile` is ignored by Docker builds and is kept
only for buildpack-style deployments). `python -m app.entrypoint` is the container command.

## Process model

`SERVICE_ROLE` selects what runs:

| Role | Runs |
|------|------|
| `all` (default) | Alembic migration, then uvicorn (API) and the worker under a supervisor |
| `web` | Alembic migration, then uvicorn only |
| `worker` | Worker only (no migration) |

Behaviour worth knowing:

- Without `DATABASE_URL` the API still starts (so a deploy cannot crash-loop) but `/health`
  returns 503 `degraded` and no worker or migration runs.
- If the migration fails the API stays up as degraded and the worker is not started.
- The supervisor restarts a crashed worker with back-off (2s up to 60s). A web exit is fatal so
  Railway restarts the container.
- `/live` is a pure liveness probe (used as the Railway healthcheck). `/health` is readiness:
  it checks the database and that the Alembic revision matches the code.

## Required setup (needs the owner)

1. Add a PostgreSQL service to the project (this is a paid resource).
2. On the app service set `DATABASE_URL=${{Postgres.DATABASE_URL}}`.
3. Set `API_KEY` and `READ_API_KEY` yourself in Railway (never commit them).
4. Recommended: `HTTP_CONCURRENCY=2`, `REQUEST_DELAY_MS=500`, `SCHEDULE_TIMEZONE=UTC`,
   `DISCOVERY_CRON="0 2 * * *"`, `STOCK_CRON="0 */4 * * *"`, `ENVIRONMENT=production`.
5. First crawl: set `BOOTSTRAP_DISCOVERY=true`. The worker enqueues exactly one discovery job when
   no discovery job or run has ever existed; restarts never create duplicates.
6. Generate the public domain only after `READ_API_KEY` is set.

## Schedules

Cron expressions are evaluated in `SCHEDULE_TIMEZONE`. Examples: `0 2 * * *` at UTC is 07:00 in
Asia/Karachi; to run at 02:00 Karachi time set `SCHEDULE_TIMEZONE=Asia/Karachi`. Scheduling is
stateless: a schedule is due when its latest fire time has no job created at or after it, so a
redeploy or outage produces one catch-up job, not a burst.

## Source access

Digisound may block non-EU IPs with HTTP 403. The crawler never bypasses this: a 403/challenge
fails the run as `failed` and is recorded. If it happens from the Railway region, move the
service to an EU region (Amsterdam) after cost approval, or obtain access from the site owner.

## Completeness

A discovery run is `complete` only when every discovered category finished pagination, matched
the source-reported count, and had no failed requests. Otherwise it is `incomplete` and the job
is marked failed with a reason. Products are never deleted or deactivated by a run.
