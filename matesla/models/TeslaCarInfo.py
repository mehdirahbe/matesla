import datetime
from django.db import models

from matesla.VinAnalysis import IsDualMotor, GetYearFromVin
from matesla.models.VinHash import HashTheVin


# Data to save about car info which cannot change


class TeslaCarInfo(models.Model):
    vin = models.TextField()  # vin of the car
    hashedVin = models.TextField(null=True)  # hash of the vin, to use in URL of graphs
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
    # EPA range in miles, estimated from info in vin, used to
    # compute battery degradation
    EPARange = models.IntegerField(null=True)
    isDualMotor = models.BooleanField(null=True)  # Computed from vin
    modelYear = models.IntegerField(null=True)

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        indexes = [
            # for SaveIfDontExistsYet
            models.Index(fields=['vin']),
            models.Index(fields=['hashedVin']),
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
            models.Index(fields=['EPARange']),
            models.Index(fields=['isDualMotor']),
            models.Index(fields=['modelYear']),
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
            models.Index(fields=['EPARange', 'car_type']),
            models.Index(fields=['isDualMotor', 'car_type']),
            models.Index(fields=['modelYear', 'car_type']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['vin'], name='TeslaCarInfo: unique version of each car')
        ]

    #save or refresh, return saved object
    def SaveIfDontExistsYet(self, vin, context):
        if TeslaCarInfo.objects.filter(vin=vin).count() == 0:
            # Add the car
            self.vin = vin
            self.hashedVin = HashTheVin(vin)
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
            # will be filled when battery degradation is computed first time
            self.EPARange = None
            self.isDualMotor = IsDualMotor(vin)
            self.modelYear = GetYearFromVin(vin)
            # some cars don't have info-->just skip
            vehicle_state = context['vehicle_state']
            try:
                self.sentry_mode_available = vehicle_state['sentry_mode_available']
            except Exception:
                self.sentry_mode_available = False
            try:
                self.smart_summon_available = vehicle_state['smart_summon_available']
            except Exception:
                self.smart_summon_available = False
            self.save()
            return self
        else:
            # update the last seen date
            previousEntry = TeslaCarInfo.objects.filter(vin=vin)[0]
            previousEntry.LastSeenDate = datetime.datetime.now()
            # in the past we had none fields, but this is not nice is graphs-->put false instead
            if previousEntry.sentry_mode_available is None:
                previousEntry.sentry_mode_available = False
            if previousEntry.smart_summon_available is None:
                previousEntry.smart_summon_available = False
            if previousEntry.isDualMotor is None:
                previousEntry.isDualMotor = IsDualMotor(vin)
            if previousEntry.modelYear is None:
                previousEntry.modelYear = GetYearFromVin(vin)
            # Save
            previousEntry.save()
            return previousEntry
