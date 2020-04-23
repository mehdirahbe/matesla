import datetime

from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator

from matesla.TeslaConnect import GetTokenFromLoginPW, GetVehicles, GetVehicle
from matesla.models import TeslaToken


class DesiredChargeLevelForm(forms.Form):
    DesiredChargeLevel = forms.IntegerField(label='Desired Charge Level',
                                            validators=[MinValueValidator(50), MaxValueValidator(100)])


class AddTeslaAccountForm(forms.Form):
    # required=False means that the field can be left empty
    TeslaUser = forms.CharField(widget=forms.TextInput, required=False)
    TeslaPassword = forms.CharField(widget=forms.PasswordInput, required=False)
    Token = forms.CharField(widget=forms.TextInput, max_length=64, required=False)
    TokenRefresh = forms.CharField(widget=forms.TextInput, max_length=64, required=False)
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
                #init vehicle
                self.token.vehicle_id = GetVehicle(GetVehicles(self.token.access_token))
                return True  # Yes, valid
        # try on token
        if (len(self.data['Token']) == 64 and len(self.data['TokenRefresh']) == 64):
            if len(self.data['CreateAt']) < 0:
                return False
            # do we have valid creation date?
            try:
                CreateAtVal = int(self.data['CreateAt'])
                if CreateAtVal < 0:
                    return False
            except ValueError:
                return False
            vehicles = GetVehicles(self.data['Token'])
            if vehicles is not None:  # got vehicles, yes, valid
                self.token = TeslaToken()
                self.token.access_token = self.data['Token']
                self.token.created_at = CreateAtVal
                self.token.refresh_token = self.data['TokenRefresh']
                self.token.vehicle_id = GetVehicle(vehicles)
                self.token.expires_in = 45 * 86400  # 45 days, in s
                return True
        # something went wrong, not valid
        return False

    def SaveModdel(self,user):
        if self.token is None:
            return
        self.token.user_id = user
        self.token.save()
