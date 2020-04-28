from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from anonymisedstats.urls import urlpatterns


class AccountsTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is anonymisedstats.urls is empty')


    def test_UrlWorks(self):
        c = Client()
        response = c.post('/anonymisedstats/firmwareupdates')
        self.assertEqual(response.status_code, 200, 'anonymisedstats did fail')
