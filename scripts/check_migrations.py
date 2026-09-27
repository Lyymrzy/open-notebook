"""Apply the whole migration chain to a throwaway namespace, then roll it back.

Why this exists
---------------
Asserting on migration *text* in the test suite cannot catch a SurrealQL syntax
error - only executing the migration can. Migration 25 shipped a multi-table
relation declaration (`TYPE RELATION IN note, memory_item OUT source, ...`) that
SurrealDB v2 rejects with a parse error. Every unit test was green, and the
failure only appeared when the image ran against a real database - where, because
the API refuses to start after a failed migration, it took the deployment down.

This script is the cheap pre-flight that would have caught it: it runs the app's
own `AsyncMigrationManager` against an isolated namespace, so a broken migration
fails here instead of on real data.

Usage
-----
    uv run python scripts/check_migrations.py
    uv run python scripts/check_migrations.py --keep      # leave the probe schema
    uv run python scripts/check_migrations.py --all-down  # roll back the whole chain

Requires a reachable SurrealDB. It reads SURREAL_URL / SURREAL_USER /
SURREAL_PASSWORD / SURREAL_NAMESPACE / SURREAL_DATABASE, loading `.env` from the
repo root when present (same variables the app uses), and defaults to the local
dev instance. It refuses to run against the namespace the environment is
configured for, so it can never modify the real database by accident.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

PROBE_NAMESPACE = "migration_check_probe"


def _bootstrap_env() -> None:
    """Load the repo's .env (if any) and pin the probe namespace."""
    repo_root = Path(__file__).resolve().parent.parent
    os.chdir(repo_root)

    env_path = repo_root / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path)
        except ImportError:  # pragma: no cover - dotenv ships with the app deps
            pass

    os.environ.setdefault("SURREAL_URL", "ws://127.0.0.1:8000/rpc")
    os.environ.setdefault("SURREAL_USER", "root")
    os.environ.setdefault("SURREAL_PASSWORD", "password")


async def _run(probe_namespace: str, all_down: bool, keep: bool) -> int:
    from open_notebook.database.async_migrate import AsyncMigrationManager
    from open_notebook.database.repository import repo_query

    manager = AsyncMigrationManager()
    total = len(manager.up_migrations)
    failures = 0

    print(f"probe namespace : {probe_namespace}")
    print(f"database url    : {os.environ.get('SURREAL_URL')}")
    print(f"migrations      : {total}")

    print("\n-- applying every migration to an empty schema --")
    try:
        await manager.run_migration_up()
    except Exception as exc:
        print(f"FAIL applying migrations: {exc}")
        failures += 1
        return _report(failures, probe_namespace, keep)

    version = await manager.get_current_version()
    print(f"version after up: {version}")
    if version != total:
        print(f"FAIL expected version {total}, got {version}")
        failures += 1

    print(f"\n-- rolling back ({'all' if all_down else 'the last migration'}) --")
    try:
        steps = total if all_down else 1
        for _ in range(steps):
            await manager.runner.run_one_down()
        version = await manager.get_current_version()
        print(f"version after down: {version}")
        expected = 0 if all_down else total - 1
        if version != expected:
            print(f"FAIL expected version {expected}, got {version}")
            failures += 1
    except Exception as exc:
        print(f"FAIL rolling back: {exc}")
        failures += 1

    if not keep:
        try:
            await repo_query(f"REMOVE NAMESPACE IF EXISTS {probe_namespace};")
            print("\nprobe namespace removed")
        except Exception as exc:
            print(f"\ncould not remove the probe namespace ({exc}) - harmless")

    return _report(failures, probe_namespace, keep)


def _report(failures: int, probe_namespace: str, keep: bool) -> int:
    if failures:
        print(f"\nRESULT: {failures} FAILURE(S)")
        if not keep:
            print("(the probe namespace may still exist - check before re-running)")
        return 1
    print("\nRESULT: PASS - the migration chain applies and rolls back cleanly")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--namespace",
        default=PROBE_NAMESPACE,
        help=f"throwaway namespace to use (default: {PROBE_NAMESPACE})",
    )
    parser.add_argument(
        "--all-down",
        action="store_true",
        help="roll back every migration instead of only the last one",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the probe namespace instead of removing it",
    )
    args = parser.parse_args()

    _bootstrap_env()

    configured = os.environ.get("SURREAL_NAMESPACE")
    if args.namespace == configured:
        print(
            f"refusing to run: probe namespace '{args.namespace}' is the namespace "
            "this environment is configured for. Pass --namespace with something "
            "else - this script creates and drops schema."
        )
        return 2

    os.environ["SURREAL_NAMESPACE"] = args.namespace
    os.environ["SURREAL_DATABASE"] = args.namespace

    return asyncio.run(_run(args.namespace, args.all_down, args.keep))


if __name__ == "__main__":
    sys.exit(main())
