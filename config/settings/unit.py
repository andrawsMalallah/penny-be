from django.core.management.utils import get_random_secret_key

from .base import *

# Ephemeral key and dummy backend: unit checks cannot connect to development data.
SECRET_KEY = get_random_secret_key()
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
