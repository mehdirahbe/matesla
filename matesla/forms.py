from django import forms
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils.translation import gettext_lazy as _

from .models.TeslaAppSettings import TeslaAppSettings

EU_API = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
NA_API = "https://fleet-api.prd.na.vn.cloud.tesla.com"
CN_API = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"


class DesiredChargeLevelForm(forms.Form):
    DesiredChargeLevel = forms.IntegerField(
        label=_("Desired Charge Level"),
        validators=[MinValueValidator(50), MaxValueValidator(100)],
    )


class DesiredTemperatureForm(forms.Form):
    DesiredTemperature = forms.IntegerField(
        label=_("Driver temperature"),
        validators=[MinValueValidator(15), MaxValueValidator(28)],
    )


class RemoteStartDriveForm(forms.Form):
    TeslaPassword = forms.CharField(
        widget=forms.PasswordInput,
        label=_("Please enter your Tesla account password"),
    )


class TeslaAppSettingsForm(forms.ModelForm):
    """Developer app credentials (MyRobotCar) — saved once in the DB."""

    class Meta:
        model = TeslaAppSettings
        fields = ("client_id", "client_secret", "redirect_uri", "api_base", "partner_domain")
        labels = {
            "client_id": _("Client ID"),
            "client_secret": _("Client secret"),
            "redirect_uri": _("OAuth redirect URI"),
            "api_base": _("Fleet API region"),
            "partner_domain": _("Domaine partner (HTTPS public)"),
        }
        help_texts = {
            "partner_domain": _(
                "Ex. robotcar.mondomaine.be — PAS localhost. "
                "Doit aussi être en Allowed Origin sur developer.tesla.com"
            ),
        }
        widgets = {
            "client_id": forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}),
            "client_secret": forms.PasswordInput(
                attrs={"class": "form-control", "autocomplete": "new-password"},
                render_value=True,
            ),
            "redirect_uri": forms.URLInput(attrs={"class": "form-control"}),
            "api_base": forms.Select(
                choices=[
                    (EU_API, _("Europe / Middle East / Africa (recommended for Belgium)")),
                    (NA_API, _("North America / Asia-Pacific (excl. China)")),
                    (CN_API, _("China")),
                ],
                attrs={"class": "form-control"},
            ),
            "partner_domain": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "robotcar.example.com",
                    "autocomplete": "off",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["redirect_uri"].initial = "http://localhost:8001/oauth/callback"
            self.fields["api_base"].initial = EU_API

    def clean_partner_domain(self):
        domain = (self.cleaned_data.get("partner_domain") or "").strip().lower()
        domain = domain.removeprefix("https://").removeprefix("http://").split("/")[0]
        return domain
