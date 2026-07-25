from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("matesla", "0035_partner_domain"),
    ]

    operations = [
        migrations.DeleteModel(name="SuperchargerUse"),
        migrations.DeleteModel(name="LastRequestSuperchargers"),
        migrations.DeleteModel(name="AllSuperchargers"),
    ]
