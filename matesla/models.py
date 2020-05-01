from django.contrib.auth import get_user_model
from django.core.exceptions import ObjectDoesNotExist
from django.db import models
import datetime
from django.core.validators import MinLengthValidator

# don't forget to add your models in admin.py of you want an administration page

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


class TeslaToken(models.Model):
    user_id = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET(get_sentinel_user))
    access_token = models.TextField()
    expires_in = models.IntegerField(null=True)
    created_at = models.IntegerField(null=True)
    refresh_token = models.TextField(null=True)
    # the id of the first vehicle
    vehicle_id = models.TextField(null=True)
    class Meta:
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['user_id', 'access_token'], name='1 token per user')
        ]


# Data to save about firmware updates
class TeslaFirmwareHistory(models.Model):
    vin = models.TextField()  # vin of the car
    Version = models.TextField()
    Date = models.DateField()  # First date where that version was detected
    CarModel = models.TextField()  # IE model3
    IsArchive = models.BooleanField(default=False)  # true if archived, means not current on the car anymore

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        # I also know that postgress has its own logic to decide if indexes are yo be user
        # as anyway it has to check in table that row is still valid
        # so it may prefer to do a table scan even when index exists
        indexes = [
            # for SaveIfDontExistsYet
            models.Index(fields=['vin', 'IsArchive']),
            models.Index(fields=['vin', 'vin']),
            # as archives are excluded
            models.Index(fields=['IsArchive', 'Version']),
            # with date for sort by most recent
            models.Index(fields=['IsArchive', 'Version', 'Date']),
            # and for filtering by car
            models.Index(fields=['IsArchive', 'CarModel', 'Version']),
            # with date for sort by most recent
            models.Index(fields=['IsArchive', 'CarModel', 'Version', 'Date']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['vin', 'Version', 'Date'], name='unique version at same date for car')
        ]

    def SaveIfDontExistsYet(self, newvin, newversion, newcarmodel):
        if TeslaFirmwareHistory.objects.filter(vin=newvin). \
                filter(Version=newversion).count() == 0:
            # Archive eventual previous version
            previousVersions = TeslaFirmwareHistory.objects.filter(vin=newvin). \
                filter(IsArchive=False)
            for previousEntry in previousVersions:
                previousEntry.IsArchive = True
                previousEntry.Save()
            # save it
            self.vin = newvin
            self.Version = newversion
            self.CarModel = newcarmodel
            self.Date = datetime.datetime.now()
            self.IsArchive = False
            self.save()


class TeslaState:
    vin = None
    name = None
    batterydegradation = 0.
    location = ""
    isOnline: bool = True
    vehicle_state = None
    OdometerInKm = 0.
