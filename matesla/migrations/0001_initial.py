# Generated by Django 2.2.6 on 2020-04-16 18:57

from django.conf import settings
import django.core.validators
from django.db import migrations, models
import matesla.models.TeslaToken


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='TeslaToken',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('access_token', models.TextField()),
                ('expires_in', models.IntegerField()),
                ('created_at', models.IntegerField()),
                ('refresh_token', models.TextField()),
                ('vehicle_id', models.TextField()),
                ('user_id', models.ForeignKey(null=True, on_delete=models.SET(
                    matesla.models.TeslaToken.get_sentinel_user), to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='TeslaAccount',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('TeslaUser', models.TextField(validators=[django.core.validators.MinLengthValidator(3)])),
                ('TeslaPassword', models.TextField(validators=[django.core.validators.MinLengthValidator(1)])),
                ('user_id', models.ForeignKey(null=True, on_delete=models.SET(
                    matesla.models.TeslaToken.get_sentinel_user), to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
