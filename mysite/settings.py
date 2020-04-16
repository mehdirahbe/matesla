"""
Django settings for mysite project.

Generated by 'django-admin startproject' using Django 2.2.6.

For more information on this file, see
https://docs.djangoproject.com/en/2.2/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/2.2/ref/settings/
"""

# deploy: https://developer.mozilla.org/en-US/docs/Learn/Server-side/Django/Deployment

import os
import dj_database_url

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
ALLOWED_HOSTS = ['fathomless-scrubland-30645.herokuapp.com', '127.0.0.1']

# Application definition

INSTALLED_APPS = [
    'matesla.apps.MateslaConfig',
    'accounts.apps.AccountsConfig',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
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

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
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

# Internationalization
# https://docs.djangoproject.com/en/2.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_L10N = True

USE_TZ = True

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
