"""Tests for the container entrypoint plan (which processes run for which environment)."""

import pytest

from app.entrypoint import build_plan


def names(plan) -> list[str]:  # type: ignore[no-untyped-def]
    return [p.name for p in plan.processes]


class TestBuildPlan:
    def test_default_without_database_serves_api_only(self) -> None:
        plan = build_plan({}, python="py")
        assert plan.role == "all"
        assert names(plan) == ["web"]
        assert plan.run_migrations is False
        assert any("DATABASE_URL" in n for n in plan.notes)

    def test_all_with_database_runs_migrations_web_and_worker(self) -> None:
        plan = build_plan({"DATABASE_URL": "postgresql://x"}, python="py")
        assert names(plan) == ["web", "worker"]
        assert plan.run_migrations is True
        assert plan.migration_argv == ["py", "-m", "alembic", "upgrade", "head"]

    def test_web_honours_platform_port(self) -> None:
        plan = build_plan({"DATABASE_URL": "x", "SERVICE_ROLE": "web", "PORT": "5555"}, python="py")
        assert names(plan) == ["web"]
        argv = plan.processes[0].argv
        assert argv[argv.index("--port") + 1] == "5555"
        assert argv[argv.index("--host") + 1] == "0.0.0.0"
        assert plan.processes[0].fatal_on_exit is True

    def test_worker_role_does_not_migrate_unless_asked(self) -> None:
        plan = build_plan({"DATABASE_URL": "x", "SERVICE_ROLE": "worker"}, python="py")
        assert names(plan) == ["worker"] and plan.run_migrations is False
        forced = build_plan(
            {"DATABASE_URL": "x", "SERVICE_ROLE": "worker", "RUN_MIGRATIONS": "true"}, python="py"
        )
        assert forced.run_migrations is True

    def test_worker_without_database_has_nothing_to_run(self) -> None:
        plan = build_plan({"SERVICE_ROLE": "worker"}, python="py")
        assert plan.processes == []

    def test_invalid_role_rejected(self) -> None:
        with pytest.raises(ValueError):
            build_plan({"SERVICE_ROLE": "everything"})

    def test_plan_never_contains_environment_values(self) -> None:
        plan = build_plan(
            {"DATABASE_URL": "postgresql://user:SECRETPW@h/db", "API_KEY": "SECRETKEY"}
        )
        blob = repr(plan)
        assert "SECRETPW" not in blob and "SECRETKEY" not in blob
