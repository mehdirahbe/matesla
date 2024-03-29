"""mysite URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/2.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.conf.urls import url
from django.conf.urls.i18n import i18n_patterns
from django.contrib import admin
from django.urls import include, path

from mysite import settings

'''For enabling multiple languages in admin panel, we’ll prefer i18n_patterns
function and modify our root urls.py as shown below:
The i18n_patterns will automatically prepend the current active language
code to all URL patterns defined within i18n_patterns(). So, all your admin URLs,
with the current configuration having zh-cn and en activated, will have URLs as:
/en/admin/*
/zh-cn/admin/*'''

urlpatterns = [
path('carimage/', include('carimage.urls')),
]


urlpatterns += i18n_patterns(
    path('anonymisedstats/', include('anonymisedstats.urls')),
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('accounts/', include('django.contrib.auth.urls')),
    # TemplateView.as_view(template_name='the file to use.html')
    path('', include('matesla.urls')),
    path('personalstats/', include('personalstats.urls')),
    path('SuCStats/', include('SuCStats.urls'))
)

# see https://django-debug-toolbar.readthedocs.io/en/latest/installation.html
if settings.DEBUG:
    import debug_toolbar
    urlpatterns = [
        path('__debug__/', include(debug_toolbar.urls)),
    ] + urlpatterns
