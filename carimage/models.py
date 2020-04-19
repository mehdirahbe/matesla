from django.db import models


class TeslaImage(models.Model):
    #upload_to=directory where files are stored
    image_file = models.ImageField(upload_to='imagesfromteslacache')
    image_url = models.URLField()
