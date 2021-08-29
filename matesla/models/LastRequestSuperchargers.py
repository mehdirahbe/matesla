import datetime
from django.db import models

# Data to know when a car did show its available supercharger for the last time
# Goal is to avoid calling every few seconds


class LastRequestSuperchargers(models.Model):
    vin = models.TextField()  # vin of the car
    Date = models.DateTimeField()  # Date of the last call

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        # I also know that postgress has its own logic to decide if indexes are yo be user
        # as anyway it has to check in table that row is still valid
        # so it may prefer to do a table scan even when index exists
        indexes = [
            # to search by vin
            models.Index(fields=['vin'])
        ]
       # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['vin'], name='LastRequestSuperchargers: unique version of each car')
        ]

    #save or refresh, return saved object
    def SaveIfDontExistsYet(self, vin):
        if LastRequestSuperchargers.objects.filter(vin=vin).count() == 0:
            # Add the car
            self.vin = vin
            self.Date = datetime.datetime.now(datetime.timezone.utc)
            self.save()
            return self
        else:
            # update the last seen date
            previousEntry = LastRequestSuperchargers.objects.filter(vin=vin)[0]
            previousEntry.Date = datetime.datetime.now(datetime.timezone.utc)
            # Save
            previousEntry.save()
            return previousEntry
