from django.conf import settings


def support_email(request):
    return {"support_email": getattr(settings, "DEFAULT_FROM_EMAIL", "")}


def contact_info(request):
    return {"contact_info": {}}


def identifier_resolver_domain(request):
    return {"identifier_resolver_domain": getattr(settings, "IDENTIFIER_RESOLVER_DOMAIN", "")}


def default_app_font_size(request):
    return {"default_app_font_size": "16px"}


def user_permissions(request):
    return {"user_permissions": {}}


def page_states(request):
    return {"page_states": {}}


def bytez_key(request):
    # Exposed to the browser so the assistant can call Bytez directly.
    # Make sure you use the correct Bytez key for your environment.
    return {"bytez_api_key": getattr(settings, "BYTEZ_API_KEY", "")}
