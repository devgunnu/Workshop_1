"""Temporary phase-1 API Gateway and Lambda connectivity handler."""

import json
import os

EXPECTED_METHOD = "GET"
EXPECTED_PATH = "/workshop"
EXPECTED_RESOURCE = "/workshop"
EXPECTED_STAGE = "prod"
ALLOWED_METHODS = "GET,OPTIONS"


def response(status_code, payload):
    """Build the phase-1 Lambda proxy response with its CORS headers."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "null"),
            "Access-Control-Allow-Headers": "content-type,accept",
            "Access-Control-Allow-Methods": ALLOWED_METHODS,
        },
        "body": json.dumps(payload, separators=(",", ":")),
    }


def lambda_handler(event, context):
    """Respond only to the temporary phase-1 workshop route."""
    request_context = event.get("requestContext") if isinstance(event, dict) else None
    if (
        not isinstance(event, dict)
        or event.get("httpMethod") != EXPECTED_METHOD
        or event.get("path") != EXPECTED_PATH
        or event.get("resource") != EXPECTED_RESOURCE
        or not isinstance(request_context, dict)
        or request_context.get("stage") != EXPECTED_STAGE
    ):
        return response(404, {"error": {"code": "NOT_FOUND", "message": "Route not found."}})

    return response(200, {"message": "Lambda is connected"})
