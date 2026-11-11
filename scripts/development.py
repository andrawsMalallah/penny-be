"""Development and tests share the existing local penny database and tables."""

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from dotenv import set_key

from config.environment import LOCAL_ENVIRONMENT_FILE, load_local_environment

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=["configure", "setup", "check", "test", "runserver", "jobs"]
    )
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Test module/class label; repeat for focused checks",
    )
    parser.add_argument("--port", type=int, default=8000, help="Loopback development server port")
    arguments = parser.parse_args()
    if arguments.label and arguments.action != "test":
        parser.error("--label is supported only with test.")
    if not 1 <= arguments.port <= 65535:
        parser.error("Use a valid local server port.")
    load_local_environment()
    if arguments.action == "configure":
        password = getpass.getpass("Existing local PostgreSQL password (saved only in .env): ")
        if not password:
            raise SystemExit("Password cannot be empty. Configuration unchanged.")
        set_key(str(LOCAL_ENVIRONMENT_FILE), "PENNY_DB_PASSWORD", password, quote_mode="always")
        print("Local database password saved in the gitignored .env. No database changes made.")
        return 0
    password = os.environ.get("PENNY_DB_PASSWORD") or getpass.getpass(
        "Local PostgreSQL password (not saved by this command): "
    )
    if not os.environ.get("PENNY_SECRET_KEY"):
        raise SystemExit(
            "Configure the persistent PENNY_SECRET_KEY before starting local commands."
        )
    environment = os.environ.copy()
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "config.settings.local",
            "PENNY_DB_NAME": "penny",
            "PENNY_DB_USER": os.environ.get("PENNY_DB_USER", "postgres"),
            "PENNY_DB_PASSWORD": password,
            "PENNY_DB_HOST": "localhost",
            "PENNY_DB_PORT": "5432",
            "PENNY_DB_SSLMODE": "disable",
            "PENNY_SECRET_KEY": os.environ["PENNY_SECRET_KEY"],
        }
    )

    def run_command(*command: str) -> int:
        try:
            return subprocess.run(
                [sys.executable, "manage.py", *command],
                cwd=BACKEND_ROOT,
                env=environment,
                check=False,
            ).returncode
        except KeyboardInterrupt:
            # Ctrl+C reaches both console processes; subprocess.run handles child cleanup.
            return 130

    if arguments.action == "test":
        print("Tests use local penny development tables; cleanup may clear development records.")
        return run_command(
            "test",
            *arguments.label,
            "--settings=config.settings.test",
            "--verbosity=2",
            "--noinput",
        )
    if arguments.action == "jobs":
        return run_command("process_financial_jobs", "--limit", "20")
    if arguments.action == "setup":
        result = run_command("migrate", "--plan")
        if result:
            return result
        result = run_command("migrate", "--noinput")
        if result:
            return result
    if arguments.action == "runserver":
        return run_command("runserver", f"127.0.0.1:{arguments.port}", "--noreload")
    result = run_command("check")
    return result or run_command("migrate", "--check")


if __name__ == "__main__":
    raise SystemExit(main())
