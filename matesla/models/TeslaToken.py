from django.contrib.auth import get_user_model
from django.db import models

# see https://stackoverflow.com/questions/50874470/how-to-save-signed-in-username-with-the-form-to-database-django
def get_sentinel_user():
    return get_user_model().objects.get_or_create(username='deleted')[0]


class TeslaToken(models.Model):
    user_id = models.ForeignKey(get_user_model(), null=True, on_delete=models.SET(get_sentinel_user))
    access_token = models.TextField()
    # the id of the first vehicle
    vehicle_id = models.TextField(null=True)

    class Meta:
        # avoid having dups in db
        constraints = [
            models.UniqueConstraint(fields=['user_id', 'access_token'], name='1 token per user')
        ]


