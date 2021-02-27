import datetime
from django.db import models

# Data to save location and name of a supercharger
from matesla.models.AllSuperchargers import AllSuperchargers


class SuperchargerUse(models.Model):
    available_stalls = models.IntegerField()
    total_stalls = models.IntegerField()
    site_closed = models.BooleanField()
    Date = models.DateField()  # Date of the data
    # allow to find the supercharger in AllSuperchargers table
    superchargerfkey = models.ForeignKey(AllSuperchargers, on_delete=models.CASCADE)

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        # I also know that postgress has its own logic to decide if indexes are yo be user
        # as anyway it has to check in table that row is still valid
        # so it may prefer to do a table scan even when index exists
        indexes = [
            # to search by size
            models.Index(fields=['total_stalls'])
        ]

    # save
    def Save(self, newavailable_stalls,newtotal_stalls,newsite_closed,newsuperchargerfkey):
            self.available_stalls = newavailable_stalls
            self.total_stalls = newtotal_stalls
            self.site_closed = newsite_closed
            self.superchargerfkey = newsuperchargerfkey
            self.Date = datetime.datetime.now()
            self.save()
