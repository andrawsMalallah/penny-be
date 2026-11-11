"""Supervise a small WSGI server and a financial worker in one Linux service."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def stop_children(children: list[subprocess.Popen], grace_seconds: float = 20) -> None:
    """Stop process groups too, so a failed worker cannot leave an orphaned web server."""
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    for child in children:
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    # The group may still contain Gunicorn workers even after its master has exited.
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def main() -> int:
    if sys.platform == "win32":
        print("This launcher requires Linux/Unix; use the documented local development commands.")
        return 1
    environment = os.environ.copy()
    environment["DJANGO_SETTINGS_MODULE"] = "config.settings.production"
    environment["PYTHONUNBUFFERED"] = "1"
    port = environment.get("PORT", "10000")
    if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
        print("Set PORT to a valid TCP port.")
        return 1
    stopped = Event()
    children: list[subprocess.Popen] = []
    previous_handlers = {}

    def stop(signum, frame) -> None:
        stopped.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, stop)
    try:
        # Startup is read-only: new production migrations require separate explicit approval.
        for check in (["check", "--deploy", "--fail-level", "ERROR"], ["migrate", "--check"]):
            child = subprocess.Popen(
                [sys.executable, "manage.py", *check],
                cwd=BACKEND_ROOT,
                env=environment,
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            children.append(child)
            while child.poll() is None:
                if stopped.wait(0.25):
                    return 0
            if child.returncode:
                print(
                    f"Startup {check[0]} check failed; review configuration and migration status."
                )
                return 1
            children.remove(child)
            if stopped.is_set():
                return 0
        for command in (
            [
                sys.executable,
                "-m",
                "gunicorn",
                "--config",
                "config/gunicorn.py",
                "config.wsgi:application",
            ],
            [sys.executable, "manage.py", "process_financial_jobs", "--watch"],
        ):
            if stopped.is_set():
                return 0
            children.append(
                subprocess.Popen(command, cwd=BACKEND_ROOT, env=environment, start_new_session=True)
            )
        print("Web server and financial worker started.", flush=True)
        while not stopped.wait(1):
            if any(child.poll() is not None for child in children):
                print("A service process exited; stopping both processes.", flush=True)
                return 1
        return 0
    except OSError:
        print("Unable to start service processes.", flush=True)
        return 1
    finally:
        stop_children(children)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    raise SystemExit(main())
