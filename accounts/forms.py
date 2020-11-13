from django import forms
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.translation import ugettext_lazy as _

class SignUpForm(UserCreationForm):
    email = forms.EmailField(max_length=254, help_text=_('Required to be able to reset password if you forget it'))

    class Meta:
        model = User
        fields = ('username', 'email', 'password1', 'password2')