from django.test import Client, TestCase

from accounts.urls import urlpatterns
from mysite.test_helpers import configured_language_codes


class AccountsTestCase(TestCase):
    def test_hasUrl(self):
        # Check that we have URL defined
        self.assertGreaterEqual(len(urlpatterns), 1, 'urlpatterns is accounts.urls is empty')

    def test_UrlWorks(self):
        client = Client()
        for lang in configured_language_codes():
            response = client.post("/" + lang + "/accounts/signup/")
            self.assertEqual(response.status_code, 200, lang + " signup did fail")
        response = client.post("/accounts/signup/")
        # test on 302 as it must redirect to a language
        self.assertEqual(response.status_code, 302, "Int signup did fail")
