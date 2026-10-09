from django.core.management.base import BaseCommand
from django.utils import timezone

from subscriptions.models import PolicySubscription


class Command(BaseCommand):
    help = "Mark active subscriptions whose end_date has passed as expired."

    def handle(self, *args, **options):
        n = PolicySubscription.objects.filter(status="active", end_date__lt=timezone.localdate()).update(status="expired")
        self.stdout.write(f"{n} subscription(s) expired.")
