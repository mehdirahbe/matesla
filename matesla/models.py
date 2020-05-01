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


# Data to save about car info which cannot change
class TeslaCarInfo(models.Model):
    vin = models.TextField()  # vin of the car
    Date = models.DateField()  # First date where that car was detected
    LastSeenDate = models.DateField()  # Last date where that car was seen
    # names are the ones that tesla returns
    car_type = models.TextField()  # IE model3
    charge_port_type = models.TextField()  # IE CCS
    exterior_color = models.TextField()  # IE SolidBlack
    has_air_suspension = models.BooleanField()  # IE False
    has_ludicrous_mode = models.BooleanField()  # IE False
    motorized_charge_port = models.BooleanField()  # IE True
    rear_seat_heaters = models.TextField()  # IE 1
    rhd = models.BooleanField()  # IE False
    roof_color = models.TextField()  # IE Glass
    wheel_type = models.TextField()  # IE Pinwheel18
    # for those 2 fields, we may lack info
    sentry_mode_available = models.BooleanField(null=True)  # IE True
    smart_summon_available = models.BooleanField(null=True)  # IE True
    eu_vehicle = models.BooleanField()  # IE True

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        indexes = [
            # for SaveIfDontExistsYet
            models.Index(fields=['vin']),
            # for stats on the field
            models.Index(fields=['car_type']),
            models.Index(fields=['charge_port_type']),
            models.Index(fields=['exterior_color']),
            models.Index(fields=['has_air_suspension']),
            models.Index(fields=['has_ludicrous_mode']),
            models.Index(fields=['motorized_charge_port']),
            models.Index(fields=['rear_seat_heaters']),
            models.Index(fields=['rhd']),
            models.Index(fields=['roof_color']),
            models.Index(fields=['wheel_type']),
            models.Index(fields=['sentry_mode_available']),
            models.Index(fields=['smart_summon_available']),
            models.Index(fields=['eu_vehicle']),
            # again with car type for segmentation on both criterias
            models.Index(fields=['charge_port_type', 'car_type']),
            models.Index(fields=['exterior_color', 'car_type']),
            models.Index(fields=['has_air_suspension', 'car_type']),
            models.Index(fields=['has_ludicrous_mode', 'car_type']),
            models.Index(fields=['motorized_charge_port', 'car_type']),
            models.Index(fields=['rear_seat_heaters', 'car_type']),
            models.Index(fields=['rhd', 'car_type']),
            models.Index(fields=['roof_color', 'car_type']),
            models.Index(fields=['wheel_type', 'car_type']),
            models.Index(fields=['sentry_mode_available', 'car_type']),
            models.Index(fields=['smart_summon_available', 'car_type']),
            models.Index(fields=['eu_vehicle', 'car_type']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['vin'], name='unique version of each car')
        ]

    def SaveIfDontExistsYet(self, vin, context):
        if TeslaCarInfo.objects.filter(vin=vin).count() == 0:
            # Add the car
            self.vin = vin
            self.Date = self.LastSeenDate = datetime.datetime.now()
            vehicle_config = context['vehicle_config']
            self.car_type = vehicle_config['car_type']
            self.charge_port_type = vehicle_config['charge_port_type']
            self.exterior_color = vehicle_config['exterior_color']
            self.has_air_suspension = vehicle_config['has_air_suspension']
            self.has_ludicrous_mode = vehicle_config['has_ludicrous_mode']
            self.motorized_charge_port = vehicle_config['motorized_charge_port']
            self.rear_seat_heaters = vehicle_config['rear_seat_heaters']
            self.rhd = vehicle_config['rhd']
            self.roof_color = vehicle_config['roof_color']
            self.wheel_type = vehicle_config['wheel_type']
            self.eu_vehicle = vehicle_config['eu_vehicle']
            # some cars don't have info-->just skip
            vehicle_state = context['vehicle_state']
            try:
                self.sentry_mode_available = vehicle_state['sentry_mode_available']
            except Exception:
                self.sentry_mode_available = None
            try:
                self.smart_summon_available = vehicle_state['smart_summon_available']
            except Exception:
                self.smart_summon_available = None
            self.save()
        else:
            # update the last seen date
            previousEntry = TeslaCarInfo.objects.filter(vin=vin)[0]
            previousEntry.LastSeenDate = datetime.datetime.now()
            previousEntry.save()


class TeslaState:
    vin = None
    name = None
    batterydegradation = 0.
    location = ""
    isOnline: bool = True
    vehicle_state = None
    OdometerInKm = 0.
