from django import forms
from django.utils.translation import gettext_lazy as _

from .models.TeslaAppSettings import TeslaAppSettings

EU_API = "https://fleet-api.prd.eu.vn.cloud.tesla.com"
NA_API = "https://fleet-api.prd.na.vn.cloud.tesla.com"
CN_API = "https://fleet-api.prd.cn.vn.cloud.tesla.cn"


class TeslaAppSettingsForm(forms.ModelForm):
    """Developer app credentials — saved once in the DB (step 1).

    Partner domain is edited separately (step 2) so saving credentials
    never wipes a domain that was not posted with this form.
    """

    class Meta:
        model = TeslaAppSettings
        fields = ("client_id", "client_secret", "redirect_uri", "api_base")
        labels = {
            "client_id": _("Client ID"),
            "client_secret": _("Client secret"),
            "redirect_uri": _("OAuth redirect URI"),
            "api_base": _("Fleet API region"),
        }
        help_texts = {
            "redirect_uri": _(
                "Must match the redirect URI on developer.tesla.com exactly "
                "(often http://localhost:8001/oauth/callback for a local install)."
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
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["redirect_uri"].initial = "http://localhost:8001/oauth/callback"
            self.fields["api_base"].initial = EU_API


class TeslaPartnerDomainForm(forms.Form):
    """Partner domain / public-key URL (step 2) — independent of credentials."""

    partner_domain = forms.CharField(
        label=_("Partner domain or public key URL"),
        required=False,
        max_length=512,
        help_text=_(
            "Paste a domain (example.com), an origin (https://example.com), "
            "or the full public-key URL. Hosting is free choice (nginx, S3, "
            "Cloudflare, GitHub Pages, …) but Tesla always fetches "
            "/.well-known/appspecific/com.tesla.3p.public-key.pem on that host. "
            "Add the same host with https:// as Allowed Origin on "
            "developer.tesla.com. Not localhost."
        ),
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": (
                    "https://your-domain.example/"
                    ".well-known/appspecific/com.tesla.3p.public-key.pem"
                ),
                "autocomplete": "off",
                "spellcheck": "false",
            }
        ),
    )

    def clean_partner_domain(self):
        from matesla.TeslaPartner import parse_partner_public_key_input

        raw = self.cleaned_data.get("partner_domain") or ""
        try:
            domain, _url = parse_partner_public_key_input(raw)
        except ValueError as exc:
            raise forms.ValidationError(str(exc)) from exc
        return domain
