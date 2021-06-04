from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator

from matesla.TeslaConnect import GetVehicles, GetVehicle
from .models.TeslaToken import TeslaToken
from django.utils.translation import ugettext_lazy as _

class DesiredChargeLevelForm(forms.Form):
    DesiredChargeLevel = forms.IntegerField(label=_('Desired Charge Level'),
                                            validators=[MinValueValidator(50), MaxValueValidator(100)])
class DesiredTemperatureForm(forms.Form):
    #28 °C is the max that my car accepts
    DesiredTemperature = forms.IntegerField(label=_('Driver temperature'),
                                            validators=[MinValueValidator(15), MaxValueValidator(28)])

class RemoteStartDriveForm(forms.Form):
    #Ask for tesla account password
    TeslaPassword = forms.CharField(widget=forms.PasswordInput,label=_('Please enter your Tesla account password'))

class AddTeslaAccountForm(forms.Form):
    # required=False means that the field can be left empty
    Token = forms.CharField(widget=forms.TextInput, max_length=200, required=True)
    # Token retrieved during is valid
    token = TeslaToken()

    # return True if content is valid (override of default method)
    def is_valid(self):
        self.token = None
        valid = super(AddTeslaAccountForm, self).is_valid()
        if not valid:
            return valid
        # try on token
        if len(self.data['Token']) >= 64 and len(self.data['Token']) < 200:
            vehicles = GetVehicles(self.data['Token'])
            if vehicles is not None:  # got vehicles, yes, valid
                self.token = TeslaToken()
                self.token.access_token = self.data['Token']
                # save vehicle id
                self.token.vehicle_id = GetVehicle(vehicles)
            return True
        # something went wrong, not valid
        return False

    def SaveModdel(self, user):
        if self.token is None:
            return
        self.token.user_id = user
        self.token.save()
