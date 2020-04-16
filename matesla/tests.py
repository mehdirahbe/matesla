from django.test import TestCase

#See for doc: https://docs.djangoproject.com/en/2.2/topics/testing/overview/
#to run tests: python3 manage.py test

# Create your tests here.
from matesla.passwordencryption import getSaltForKey, encrypt, decrypt

class PWEncryptDecryptTestCase(TestCase):
#    def setUp(self):

    def test_encryptedcandedecrypted(self):
        saltlogin=getSaltForKey("login")
        encryptedpw=encrypt("pwéà ", saltlogin)
        original=decrypt(encryptedpw,saltlogin)
        self.assertEqual(original, "pwéà ")

    def test_saltwork(self):
        saltlogin=getSaltForKey("login")
        self.assertNotEqual(saltlogin, "login")

    def test_encryptwork(self):
        saltlogin=getSaltForKey("login")
        encryptedpw=encrypt("pwéà ", saltlogin)
        self.assertNotEqual(encryptedpw, "pwéà ")
