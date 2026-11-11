import signal
from threading import Event

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections, connections

from apps.backups.services import process_one


class Command(BaseCommand):
    help = "Process a bounded batch of queued financial jobs; never accepts backup passwords."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--watch", action="store_true", help="Poll until terminated.")
        parser.add_argument("--poll-interval", type=int, default=10)

    def handle(self, *args, **options) -> None:
        if not 1 <= options["limit"] <= 100:
            raise CommandError("Use a job limit between 1 and 100.")
        if not 1 <= options["poll_interval"] <= 60:
            raise CommandError("Use a polling interval between 1 and 60 seconds.")
        stopped = Event()
        previous_handlers = {}

        def stop(signum, frame) -> None:
            # Finish the current atomic job before stopping; never start another one.
            stopped.set()

        if options["watch"]:
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, stop)
        try:
            while not stopped.is_set():
                count = 0
                for _ in range(options["limit"]):
                    if stopped.is_set():
                        break
                    close_old_connections()
                    if not process_one():
                        break
                    count += 1
                if count or not options["watch"]:
                    self.stdout.write(f"Processed {count} financial jobs.")
                    self.stdout.flush()
                if not options["watch"]:
                    break
                # Release idle connections; the durable queue lives in PostgreSQL.
                connections.close_all()
                stopped.wait(options["poll_interval"])
        except DatabaseError:
            raise CommandError("Financial worker lost database access.") from None
        finally:
            connections.close_all()
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
