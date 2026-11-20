"""Linux production entry point. No migrations, seeders or outbound requests at startup."""

import ipaddress
import os


def positive_integer(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise ValueError(f"{name} must be a positive integer.") from None
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} is outside the supported range.")
    return value


bind = f"0.0.0.0:{positive_integer('PORT', 8000, 65535)}"
workers = positive_integer("PENNY_WORKERS", 1, 8)
threads = positive_integer("PENNY_THREADS", 2, 8)
worker_class = "gthread"
timeout = 60
graceful_timeout = 30
max_requests = 1000
max_requests_jitter = 100
accesslog = None
errorlog = "-"

# Trust only explicitly known proxy peers; never accept arbitrary forwarded headers.
proxy_addresses = {"127.0.0.1", "::1"}
for address in os.environ.get("PENNY_TRUSTED_PROXY_IPS", "").split(","):
    if address.strip():
        proxy_addresses.add(str(ipaddress.ip_address(address.strip())))
forwarded_allow_ips = ",".join(sorted(proxy_addresses))
secure_scheme_headers = {"X-FORWARDED-PROTO": "https"}
