import datetime

from django.db import models


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
                previousEntry.save()
            # save it
            self.vin = newvin
            self.Version = newversion
            self.CarModel = newcarmodel
            self.Date = datetime.datetime.now()
            self.IsArchive = False
            self.save()