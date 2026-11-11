from config.environment import load_local_environment

from .base import *

PENNY_API_DOCS_ENABLED = True

load_local_environment()
SECRET_KEY = required_environment("PENNY_SECRET_KEY")
# base is imported before local dotenv loading; read this key after loading as well.
PENNY_MFA_ENCRYPTION_KEY = required_environment("PENNY_MFA_ENCRYPTION_KEY")
# Enable only for explicit local development; Django never defaults to debug output.
DEBUG = os.environ.get("PENNY_LOCAL_DEBUG") == "1"
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
DATABASES = {"default": postgres_database()}
