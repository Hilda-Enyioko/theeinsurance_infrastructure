import time

from django.core.management.base import BaseCommand

from webhooks.services import retry_due


class Command(BaseCommand):
    help = "Retry pending webhook deliveries."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run forever (Render background worker).")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **opts):
        while True:
            retry_due()
            if not opts["loop"]:
                break
            time.sleep(opts["interval"])
