# Generated by Django 3.0.6 on 2020-05-20 21:39

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0017_teslacardatasnapshot_battery_degradation'),
    ]

    operations = [
        migrations.AddField(
            model_name='teslacardatasnapshot',
            name='DateOnlyDay',
            field=models.DateField(null=True),
        ),
    ]
