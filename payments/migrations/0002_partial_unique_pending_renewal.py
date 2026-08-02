from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('payments', '0001_initial')]
    operations = [
        migrations.AddConstraint(
            model_name='transaction',
            constraint=models.UniqueConstraint(
                fields=['subscription', 'payment_type', 'gateway'],
                condition=models.Q(payment_status='pending'),
                name='unique_pending_renewal_per_subscription',
            ),
        ),
    ]
