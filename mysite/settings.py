"""
Django settings for mysite project.

Generated by 'django-admin startproject' using Django 2.2.6.

For more information on this file, see
https://docs.djangoproject.com/en/2.2/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/2.2/ref/settings/
"""

# deploy: https://developer.mozilla.org/en-US/docs/Learn/Server-side/Django/Deployment

# for cache, see https://docs.djangoproject.com/en/3.0/topics/cache/

import os
import dj_database_url
# for translations, from https://www.codementor.io/@curiouslearner/supporting-multiple-languages-in-django-part-1-11xjd2ovik
from django.utils.translation import ugettext_lazy as _

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'unique-snowflake',
    }
}

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/2.2/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
# from deploy, Getting your website ready to publish
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'eta1h#d8$plq6gy!2l96lbxe5k9gu7we#q*0w=gxe+=(szicm4')
saltSeed = SECRET_KEY + 'LL2SV-4tghzsrgsdgvsdgqdgvqd[_zCRxUwXYC=wsdgqdsgqdgqdgvghjjkjfCC5GCTNdE-Dsw>}bBp.'  # MAKE THIS YOUR OWN RANDOM STRING

# SECURITY WARNING: don't run with debug turned on in production!
# from deploy, Getting your website ready to publish
DEBUG = os.environ.get('DJANGO_DEBUG', '') != 'False'

# from deploy, running check --deploy, thus each warning must be corrected
# solutions from: https://stackoverflow.com/questions/36466163/why-am-i-getting-these-django-security-warnings-but-not-other-developers-on-the
# The first warning is about the HSTS header which prevents browsers from accessing the data in HTTP. It is typically set to 1 year i.e 31536000 seconds. But under testing conditions you might prefer using a lower value like 60 sec or 3600 sec.
SECURE_HSTS_SECONDS = 31536000

# The second warning is to prevent sniffing attacks. This can be prevented by adding the following line in your settings.py
SECURE_CONTENT_TYPE_NOSNIFF = True

# ?: (security.W005) You have not set the SECURE_HSTS_INCLUDE_SUBDOMAINS setting to True. Without this, your site is potentially vulnerable to attack via an insecure connection to a subdomain. Only set this to True if you are certain that all subdomains of your domain should be served exclusively via SSL.
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# ?: (security.W007) Your SECURE_BROWSER_XSS_FILTER setting is not set to True, so your pages will not be served with an 'X-XSS-Protection: 1; mode=block' header. You should consider enabling this header to activate the browser's XSS filtering and help prevent XSS attacks.
SECURE_BROWSER_XSS_FILTER = True

# ?: (security.W008) Your SECURE_SSL_REDIRECT setting is not set to True. Unless your site should be available over both SSL and non-SSL connections, you may want to either set this setting True or configure a load balancer or reverse-proxy server to redirect all connections to HTTPS.
# hardcoding true prevent running http test site, so ensure that it is true when in test site
SECURE_SSL_REDIRECT = not DEBUG

# ?: (security.W012) SESSION_COOKIE_SECURE is not set to True. Using a secure-only session cookie makes it more difficult for network traffic sniffers to hijack user sessions.
SESSION_COOKIE_SECURE = True

# ?: (security.W016) You have 'django.middleware.csrf.CsrfViewMiddleware' in your MIDDLEWARE, but you have not set CSRF_COOKIE_SECURE to True. Using a secure-only CSRF cookie makes it more difficult for network traffic sniffers to steal the CSRF token.
CSRF_COOKIE_SECURE = True

# ?: (security.W019) You have 'django.middleware.clickjacking.XFrameOptionsMiddleware' in your MIDDLEWARE, but X_FRAME_OPTIONS is not set to 'DENY'. The default is 'SAMEORIGIN', but unless there is a good reason for your site to serve other parts of itself in a frame, you should change it to 'DENY'.
X_FRAME_OPTIONS = "DENY"

# ?: (security.W021) You have not set the SECURE_HSTS_PRELOAD setting to True. Without this, your site cannot be submitted to the browser preload list.
SECURE_HSTS_PRELOAD = True

# deploy, from Setting configuration variables
ALLOWED_HOSTS = ['afternoon-scrubland-61531.herokuapp.com', 'matesla.herokuapp.com', '127.0.0.1']

# Application definition

INSTALLED_APPS = [
    'matesla.apps.MateslaConfig',
    'accounts.apps.AccountsConfig',
    'carimage.apps.CarimageConfig',
    'anonymisedstats.apps.AnonymisedstatsConfig',
    'personalstats.apps.PersonalstatsConfig',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

# Setup LocaleMiddleware to enable translations using ugettext_lazy and ugettext
# Make sure that LocaleMiddleware comes after SessionMiddleware and before CommonMiddleware
MIDDLEWARE = [
    'django.middleware.cache.UpdateCacheMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.locale.LocaleMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'django.middleware.cache.FetchFromCacheMiddleware',
]

ROOT_URLCONF = 'mysite.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'mysite.wsgi.application'
LOGIN_REDIRECT_URL = 'tesla_status'
LOGOUT_REDIRECT_URL = 'login'

# Database
# https://docs.djangoproject.com/en/2.2/ref/settings/#databases

'''To use a file as db
'default': {
    'ENGINE': 'django.db.backends.sqlite3',
    'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
}
'''

'''
To use postgres local db.
    #From https://djangocentral.com/using-postgresql-with-django/ to use postgres in local
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': 'mrtestdb',
        'USER': 'mr',
        'PASSWORD': 'mr',
        'HOST': 'localhost',
        'PORT': '5433',
    }
    
    From https://devcenter.heroku.com/articles/heroku-postgresql#pg-push-and-pg-pull
    You can make a copy of site db in local postgres with this.
    The name to use is just under Installed add-ons on heroku dashboard
    heroku pg:pull postgresql-...  mrtestdb  --app afternoon-scrubland-61531

    Postgress 12 should have been installed first and user mr (put your ream user)
    created with ad hoc password, here dummy mr
    Installing postgress on ubuntu is not so simple, see
    https://tecadmin.net/install-postgresql-server-on-ubuntu/
    Contrary to previous versions, postgress 12 uses port 5433 (was 5432)
    
    To start postgress server, it is sudo service postgresql start
    You may have to check /etc/postgresql/12/main/pg_hba.conf
    to be able to connect in tcp
    And have a look in /var/log/postgresql/postgresql-12-main.log to see
    what it is doing.'''

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql_psycopg2',
        'NAME': 'mrtestdb',
        'USER': 'mr',
        'PASSWORD': 'mr',
        'HOST': 'localhost',
        'PORT': '5433',
    }
}

# Password validation
# https://docs.djangoproject.com/en/2.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Provide a lists of languages which your site supports.
LANGUAGES = (
    ('en', _('English')),
    ('fr', _('Français')),
    ('es', _('Espanol')),
)

# translations are generated from marked labels by running this
# django-admin makemessages -l fr
# django-admin makemessages -l es
# Once translated manually in po file, please run django-admin compilemessages
# or new translations won't be taken into account
# see https://docs.djangoproject.com/en/3.0/topics/i18n/translation/

# Internationalization
# https://docs.djangoproject.com/en/2.2/topics/i18n/

# Set the default language for your site.
LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery. Make sure it is set to
# True if you want to support localization
USE_I18N = True

# If you set this to False, Django will not format dates, numbers and
# calendars according to the current locale.
USE_L10N = True

USE_TZ = True

# Contains the path list where Django should look into for django.po files for all supported languages
LOCALE_PATHS = (
    os.path.join(BASE_DIR, 'locale'),
)

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/2.2/howto/static-files/

# The absolute path to the directory where collectstatic will collect static files for deployment.
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# The URL to use when referring to static files (where they will be served from)
STATIC_URL = '/static/'

# Heroku: Update database configuration from $DATABASE_URL.
db_from_env = dj_database_url.config(conn_max_age=500)
DATABASES['default'].update(db_from_env)

# Simplified static file serving.
# https://warehouse.python.org/project/whitenoise/
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# to serve static files not linked to an app-->CSS,...
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static")
]
