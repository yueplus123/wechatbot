from django.apps import AppConfig


class GatewayApiConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.gateway_api"
    label = "gateway_api"
    verbose_name = "网关 API"
