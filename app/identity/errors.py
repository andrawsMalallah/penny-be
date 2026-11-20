from rest_framework.exceptions import APIException
from rest_framework.views import exception_handler as drf_exception_handler


class ServiceUnavailable(APIException):
    status_code = 503
    default_detail = "Verification is temporarily unavailable. Please try again shortly."
    default_code = "service_unavailable"


class CodeRequestLimit(APIException):
    status_code = 429
    default_detail = "You can request three codes per hour. Please try again later."
    default_code = "code_request_limit"


class LoginAttemptLimit(APIException):
    status_code = 429
    default_detail = "Too many sign-in attempts. Please try again in 15 minutes."
    default_code = "login_attempt_limit"


class InvalidCredentials(APIException):
    status_code = 401
    default_detail = "Email or password is incorrect."
    default_code = "invalid_credentials"


def exception_handler(exception: Exception, context: dict):
    response = drf_exception_handler(exception, context)
    if response is not None:
        response["Cache-Control"] = "no-store"
    return response
