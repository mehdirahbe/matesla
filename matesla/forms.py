from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator

from matesla.TeslaConnect import GetVehicles, GetVehicle
from .GetTeslaToken import GetTokenFromLoginPW
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
    TeslaUser = forms.CharField(widget=forms.TextInput, required=False)
    TeslaPassword = forms.CharField(widget=forms.PasswordInput, required=False)
    Token = forms.CharField(widget=forms.TextInput, max_length=200, required=False)
    TokenRefresh = forms.CharField(widget=forms.TextInput, max_length=200, required=False)
    CreateAt = forms.IntegerField(widget=forms.TextInput, required=False)
    # Token retrieved during is valid
    token = TeslaToken()

    # return True if content is valid (override of default method)
    def is_valid(self):
        self.token = None
        valid = super(AddTeslaAccountForm, self).is_valid()
        if not valid:
            return valid
        # either, we have a login and a PW, or a token
        # try to login
        if (len(self.data['TeslaUser']) > 0 and len(self.data['TeslaPassword']) > 0):
            self.token = GetTokenFromLoginPW(self.data['TeslaUser'], self.data['TeslaPassword'])
            if self.token is not None:
                # init vehicle
                self.token.vehicle_id = GetVehicle(GetVehicles(self.token.access_token))
                return True  # Yes, valid
        # try on token
        if len(self.data['Token']) >= 64 and len(self.data['Token']) < 200:
            vehicles = GetVehicles(self.data['Token'])
            if vehicles is not None:  # got vehicles, yes, valid
                self.token = TeslaToken()
                self.token.access_token = self.data['Token']
                '''Demand from Meaban on https://forums.automobile-propre.com/topic/site-pour-se-connecter-%C3%A0-sa-voiture-22944/
                stating than not informatician are lost with refresh token and creation time
                thus allow to only use token.
                Thus story refresh and creation if we have them, do without else'''
                if (len(self.data['TokenRefresh']) >= 64 and len(self.data['TokenRefresh']) < 200):
                    self.token.refresh_token = self.data['TokenRefresh']
                else:
                    self.token.refresh_token = None
                # do we have valid creation date?
                self.token.created_at = None
                if len(self.data['CreateAt']) > 0:
                    try:
                        CreateAtVal = int(self.data['CreateAt'])
                        if CreateAtVal > 0:
                            self.token.created_at = CreateAtVal
                    except ValueError:
                        self.token.created_at = None
                # save vehicle id
                self.token.vehicle_id = GetVehicle(vehicles)
                self.token.expires_in = 45 * 86400  # 45 days, in s
            return True
        # something went wrong, not valid
        return False

    def SaveModdel(self, user):
        if self.token is None:
            return
        self.token.user_id = user
        self.token.save()
