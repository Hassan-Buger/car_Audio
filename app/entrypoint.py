"""Container entrypoint: decides which processes run in this service.

``SERVICE_ROLE`` selects the role (default ``all``):

* ``web``    - run Alembic migrations (when a database is configured) then the FastAPI server.
* ``worker`` - run the background worker only (use a second Railway service for this).
* ``all``    - migrations, then the web server **and** the worker in one container; the worker is
  restarted with back-off if it exits, and a web exit stops the container so the platform
  restarts it. This lets one paid service host both until a dedicated worker service exists.

The entrypoint never prints environment values. Without ``DATABASE_URL`` it still serves the API
(``/live`` is healthy, ``/health`` reports ``degraded``) and skips migrations and the worker, so a
deploy before the database is attached cannot crash-loop.
"""

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] entrypoint: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("entrypoint")

VALID_ROLES = ("web", "worker", "all")
WORKER_BACKOFF_START = 2.0
WORKER_BACKOFF_MAX = 60.0
SHUTDOWN_GRACE_SECONDS = 30.0


@dataclass(frozen=True)
class ProcessSpec:
    """A long-running child process."""

    name: str
    argv: list[str]
    fatal_on_exit: bool = False  # True: the container should stop if this process exits


@dataclass
class Plan:
    """What the entrypoint is going to do."""

    role: str
    run_migrations: bool
    migration_argv: list[str] = field(default_factory=list)
    processes: list[ProcessSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_plan(env: Mapping[str, str], python: str | None = None) -> Plan:
    """Compute the plan from environment variables (pure, unit-testable)."""
    py = python or sys.executable
    role = (env.get("SERVICE_ROLE") or "all").strip().lower()
    if role not in VALID_ROLES:
        raise ValueError(f"SERVICE_ROLE must be one of {VALID_ROLES}, got {role!r}")

    db_configured = bool((env.get("DATABASE_URL") or "").strip())
    notes: list[str] = []
    migrate = db_configured and (role in ("web", "all") or _truthy(env.get("RUN_MIGRATIONS")))
    if not db_configured:
        notes.append("DATABASE_URL is not set: skipping migrations and the worker")

    processes: list[ProcessSpec] = []
    if role in ("web", "all"):
        port = (env.get("PORT") or "8000").strip()
        processes.append(
            ProcessSpec(
                name="web",
                argv=[
                    py,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    port,
                    "--proxy-headers",
                ],
                fatal_on_exit=True,
            )
        )
    if role in ("worker", "all"):
        if db_configured:
            processes.append(ProcessSpec(name="worker", argv=[py, "-m", "app.worker"]))
        elif role == "worker":
            notes.append("worker role requested but DATABASE_URL is not set; nothing to run")

    return Plan(
        role=role,
        run_migrations=migrate,
        migration_argv=[py, "-m", "alembic", "upgrade", "head"] if migrate else [],
        processes=processes,
        notes=notes,
    )


class Supervisor:
    """Starts child processes, restarts the worker with back-off, forwards termination signals."""

    def __init__(self, processes: list[ProcessSpec]) -> None:
        self.specs = {p.name: p for p in processes}
        self.children: dict[str, subprocess.Popen[bytes]] = {}
        self.backoff: dict[str, float] = {}
        self.next_start: dict[str, float] = {}
        self.stopping = False

    def _start(self, name: str) -> None:
        spec = self.specs[name]
        logger.info("Starting %s: %s", name, " ".join(spec.argv[:4]))
        self.children[name] = subprocess.Popen(spec.argv)

    def stop(self, *_: object) -> None:
        if not self.stopping:
            logger.info("Termination requested; stopping children")
        self.stopping = True

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        for name in self.specs:
            self._start(name)
            self.backoff[name] = WORKER_BACKOFF_START

        exit_code = 0
        while not self.stopping:
            time.sleep(0.5)
            for name, child in list(self.children.items()):
                code = child.poll()
                if code is None:
                    continue
                spec = self.specs[name]
                if spec.fatal_on_exit:
                    logger.error("%s exited with code %s; stopping container", name, code)
                    exit_code = code or 1
                    self.stopping = True
                    break
                now = time.monotonic()
                if name not in self.next_start:
                    delay = self.backoff[name]
                    logger.error("%s exited with code %s; restarting in %.0fs", name, code, delay)
                    self.next_start[name] = now + delay
                    self.backoff[name] = min(delay * 2, WORKER_BACKOFF_MAX)
                elif now >= self.next_start[name]:
                    del self.next_start[name]
                    self._start(name)

        for child in self.children.values():
            if child.poll() is None:
                child.terminate()
        deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
        for child in self.children.values():
            try:
                child.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                child.kill()
        return exit_code


def main() -> int:
    try:
        plan = build_plan(os.environ)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    logger.info(
        "Service role=%s processes=%s migrations=%s",
        plan.role,
        [p.name for p in plan.processes] or "none",
        plan.run_migrations,
    )
    for note in plan.notes:
        logger.warning(note)

    processes = list(plan.processes)
    if plan.run_migrations:
        logger.info("Applying database migrations (alembic upgrade head)")
        result = subprocess.run(plan.migration_argv)
        if result.returncode != 0:
            logger.error(
                "Migrations failed (exit %s); worker will not start, API stays up as 'degraded'",
                result.returncode,
            )
            processes = [p for p in processes if p.name != "worker"]
        else:
            logger.info("Migrations applied")

    if not processes:
        logger.error("Nothing to run for this configuration")
        return 1

    if len(processes) == 1:
        spec = processes[0]
        logger.info("Executing %s", spec.name)
        os.execv(spec.argv[0], spec.argv)

    return Supervisor(processes).run()


if __name__ == "__main__":
    sys.exit(main())
