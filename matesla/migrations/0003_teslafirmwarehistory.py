# Generated by Django 2.2.6 on 2020-04-25 00:39

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('matesla', '0002_delete_teslaaccount'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeslaFirmwareHistory',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('vin', models.TextField()),
                ('Version', models.TextField()),
                ('Date', models.DateField()),
                ('CarModel', models.TextField()),
            ],
        ),
    ]
