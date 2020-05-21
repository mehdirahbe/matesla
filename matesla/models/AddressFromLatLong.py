from django.db import models
from django.utils.timezone import now
from geopy.geocoders import Nominatim


# Get address from latitude+longitude, avoid a nearly 1 sec lookup to geopy each time


class AddressFromLatLong(models.Model):
    latitude = models.FloatField()  # IE 50.79621
    longitude = models.FloatField()  # IE 4.335445
    address = models.TextField()
    date = models.DateField()

    class Meta:
        # index definition, see https://docs.djangoproject.com/en/3.0/ref/models/options/#django.db.models.Options.indexes
        indexes = [
            # to retrieve easily address
            models.Index(fields=['latitude', 'longitude']),
        ]
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['latitude', 'longitude'],
                                    name='AddressFromLatLong: unique address for same latitude and longiture')
        ]


def GetAddressFromLatLong(latitude, longitude):
    results = AddressFromLatLong.objects.filter(latitude=latitude).filter(longitude=longitude)
    if (len(results)) == 1:
        return results[0].address
    try:
        # not yet known-->add it
        geolocator = Nominatim(user_agent="mon app tesla")
        location = geolocator.reverse(str(latitude) + "," + str(longitude))
        add = AddressFromLatLong()
        add.latitude = latitude
        add.longitude = longitude
        add.address = location.address
        add.date = now().date()
        add.save()
        return location.address
    except Exception:
        # return unknown and don't save it of course
        return "Unknown"
