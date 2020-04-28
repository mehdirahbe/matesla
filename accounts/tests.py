from django.test import TestCase
# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/tools/
from django.test import Client

# Inspired from https://docs.djangoproject.com/en/3.0/topics/testing/overview/
# Create your tests here.
from accounts.urls import urlpatterns


class AccountsTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is accounts.urls is empty')

    def test_UrlWorks(self):
        c = Client()
        for lang in {"fr","en"}:
            response = c.post("/"+lang+'/accounts/signup/')
            self.assertEqual(response.status_code, 200, lang+' signup did fail')
        response = c.post('/accounts/signup/')
        # test on 302 as it must redirect to a language
        self.assertEqual(response.status_code, 302, 'Int signup did fail')
