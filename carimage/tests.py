from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from carimage.urls import urlpatterns


class CarImageTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is carimage.urls is empty')

    def test_UrlWorks(self):
        c = Client()
        response = c.post('/carimage/PBSB/somewheel/someCarModel')
        self.assertEqual(response.status_code, 200, 'CarImage did fail')

    def test_BadUrlFails(self):
        c = Client()
        response = c.post('/carimage/PBSB/somewheel/')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = c.post('/carimage/PBSB/someCarModel')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = c.post('/carimage/somewheel/someCarModel')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
        response = c.post('/carimage/')
        self.assertEqual(response.status_code, 404, 'Bad CarImage did work')
