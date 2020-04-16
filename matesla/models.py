from django.contrib.auth import get_user_model
from django.db import models
from django.core.validators import MinLengthValidator

'''From to clean all migrations:
https://simpleisbetterthancomplex.com/tutorial/2016/07/26/how-to-reset-migrations.html

1. Remove the all migrations files within your project

Go through each of your projects apps migration folder and remove everything inside, except the __init__.py file.

Or if you are using a unix-like OS you can run the following script (inside your project dir):

find . -path "*/migrations/*.py" -not -name "__init__.py" -delete
find . -path "*/migrations/*.pyc"  -delete

2. Drop the current database, or delete the db.sqlite3 if it is your case.
3. Create the initial migrations and generate the database schema:

python manage.py makemigrations
python manage.py migrate

And you are good to go.
'''

# Create your models here.

# see https://stackoverflow.com/questions/50874470/how-to-save-signed-in-username-with-the-form-to-database-django

def get_sentinel_user():
    return get_user_model().objects.get_or_create(username='deleted')[0]


class TeslaAccount(models.Model):
    user_id = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET(get_sentinel_user))
    TeslaUser = models.TextField(validators=[MinLengthValidator(3)])  # tesla login is an email
    TeslaPassword = models.TextField(validators=[MinLengthValidator(1)])  # force a PW


class TeslaToken(models.Model):
    user_id = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET(get_sentinel_user))
    access_token = models.TextField()
    expires_in = models.IntegerField()
    created_at = models.IntegerField()
    refresh_token = models.TextField()
    # the id of the first vehicle
    vehicle_id = models.TextField()

class TeslaState:
    vin = None
    name = None
    batterydegradation = 0.
    location = ""
    isOnline: bool=True
    vehicle_state=None
    OdometerInKm=0.
