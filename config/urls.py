from django.conf import settings
from django.urls import include, path, re_path
from django.views.generic import RedirectView
from django.views.static import serve

urlpatterns = [
    path("api/v1/gateway/", include("apps.gateway_api.urls")),
    path("panel/", include("apps.panel.urls")),
    # waitress + DEBUG=False 时 staticfiles 不会自动服务；面板只有一个 css，直接由此提供
    re_path(
        r"^static/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "static"}
    ),
    path("", RedirectView.as_view(pattern_name="panel:home", permanent=False)),
]
