from django.contrib import admin
from django.conf import settings
from django.urls import path, include, re_path
from django.views.static import serve

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("website.urls")),
]

# Keep legacy asset paths working (css/, js/, images/) without rewriting templates.
if settings.DEBUG:
    urlpatterns += [
        re_path(r"^css/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "css"}),
        re_path(r"^js/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "js"}),
        re_path(r"^images/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "images"}),
        re_path(r"^sneat-assets/(?P<path>.*)$", serve, {"document_root": settings.BASE_DIR / "templates" / "sneat" / "assets"}),
    ]
