"""Conservative settings for a single small Linux web-service instance."""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = 1
worker_class = "gthread"
threads = 2
timeout = 60
graceful_timeout = 15
max_requests = 1000
max_requests_jitter = 100
errorlog = "-"
# Avoid access logs containing URLs, query strings or user-supplied values.
accesslog = None
capture_output = False
# Django handles the explicitly opted-in proxy header. Do not separately trust other headers.
secure_scheme_headers = {}
forwarder_headers = ""
