from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator

from matesla.models import TeslaAccount


class DesiredChargeLevelForm(forms.Form):
    DesiredChargeLevel = forms.IntegerField(label='Desired Charge Level',
                                            validators=[MinValueValidator(50), MaxValueValidator(100)])


class AddTeslaAccountForm(forms.ModelForm):
    class Meta:
        model = TeslaAccount
        fields = ['TeslaUser', 'TeslaPassword']
