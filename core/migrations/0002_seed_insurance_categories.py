from django.db import migrations

def seed_categories(apps, schema_editor):
    InsuranceCategory = apps.get_model('plans', 'InsuranceCategory')
    InsuranceCategory.objects.get_or_create(
        name="travel", defaults={"description": "Travel insurance plans"}
    )
    InsuranceCategory.objects.get_or_create(
        name="motor", defaults={"description": "Motor insurance plans"}
    )

class Migration(migrations.Migration):
    dependencies = [("plans", "0001_initial")]
    operations = [migrations.RunPython(seed_categories, migrations.RunPython.noop)]
from django.db import migrations

def seed_categories(apps, schema_editor):
    InsuranceCategory = apps.get_model('plans', 'InsuranceCategory')
    InsuranceCategory.objects.get_or_create(
        name="travel", defaults={"description": "Travel insurance plans"}
    )
    InsuranceCategory.objects.get_or_create(
        name="motor", defaults={"description": "Motor insurance plans"}
    )

class Migration(migrations.Migration):
    dependencies = [("plans", "0001_initial")]
    operations = [migrations.RunPython(seed_categories, migrations.RunPython.noop)]
