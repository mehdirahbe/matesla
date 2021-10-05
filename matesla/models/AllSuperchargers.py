import datetime
from django.db import models


# Data to save location and name of a supercharger
class AllSuperchargers(models.Model):
    name = models.TextField()  # Human readable location
    type = models.TextField()  # supercharger
    latitude = models.FloatField()
    longitude = models.FloatField()
    Date = models.DateField()  # First date where that charger was detected

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        # I also know that postgress has its own logic to decide if indexes are to be used
        # as anyway it has to check in table that row is still valid
        # so it may prefer to do a table scan even when index exists
        indexes = [
            # to search by name
            models.Index(fields=['name']),
            # To ensure unicity
            models.Index(fields=['latitude', 'longitude', 'name']),
            # To help SaveIfDontExistsYet
            models.Index(fields=['type', 'latitude', 'longitude', 'name']),
            # for join with use table
            models.Index(fields=['id', 'name']),
            models.Index(fields=['id']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['latitude', 'longitude', 'name'],
                                    name='AllSuperchargers: unique version of each charger')
        ]

    # save if necessary and return pkey (id) in the table
    def SaveIfDontExistsYet(self, newname, newtype, newlatitude, newlongitude):
        if AllSuperchargers.objects.filter(name=newname). \
                filter(type=newtype).filter(latitude=newlatitude). \
                filter(longitude=newlongitude).count() == 0:
            # save it
            self.name = newname
            self.type = newtype
            self.latitude = newlatitude
            self.longitude = newlongitude
            self.Date = datetime.datetime.now()
            self.save()
            return self
        # return the pkey
        found = AllSuperchargers.objects.filter(name=newname). \
            filter(type=newtype).first()
        return found
