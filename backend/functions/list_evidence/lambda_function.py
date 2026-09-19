"""API Gateway REST API handler for listing evidence metadata records.

IAM: dynamodb:Query on the exact table; s3:GetObject on
arn:aws:s3:::<asset-bucket>/evidence/demo/*.
"""

import base64
import binascii
import json
import os

import boto3
from boto3.dynamodb.conditions import Key

EXPECTED_METHOD = "GET"
EXPECTED_PATH = "/evidence"
EXPECTED_RESOURCE = "/evidence"
EXPECTED_STAGE = "prod"
ALLOWED_METHODS = "GET,POST,OPTIONS"
REQUIRED_ENVIRONMENT = ("ALLOWED_ORIGIN", "TABLE_NAME", "ASSET_BUCKET", "DOWNLOAD_URL_EXPIRY_SECONDS")
USER_KEY = "USER#demo"
EVIDENCE_SORT_KEY_PREFIX = "EVIDENCE#"
ASSET_KEY_PREFIX = "evidence/demo/"


class InvalidRequest(ValueError):
    """Raised when a REST API Lambda proxy event cannot be parsed safely."""


class ApiRequest:
    """Normalized API Gateway REST API Lambda proxy request."""

    def __init__(self, method, path, resource, stage, body, path_parameters, query_parameters):
        self.method, self.path, self.resource, self.stage = method, path, resource, stage
        self.body, self.path_parameters, self.query_parameters = body, path_parameters, query_parameters

    @classmethod
    def from_event(cls, event):
        if not isinstance(event, dict): raise InvalidRequest("Expected an API Gateway REST API Lambda proxy event.")
        method, path, resource, request_context = event.get("httpMethod"), event.get("path"), event.get("resource"), event.get("requestContext")
        if not isinstance(method, str) or not method.strip(): raise InvalidRequest("The HTTP method is missing.")
        if not isinstance(path, str) or not path.startswith("/"): raise InvalidRequest("The request path is missing.")
        if not isinstance(resource, str) or not resource.startswith("/"): raise InvalidRequest("The request resource is missing.")
        if not isinstance(request_context, dict) or not isinstance(request_context.get("stage"), str) or not request_context["stage"].strip(): raise InvalidRequest("The request stage is missing.")
        body = event.get("body")
        if event.get("isBase64Encoded", False) and body is not None:
            if not isinstance(body, str): raise InvalidRequest("The encoded request body must be text.")
            try: body = base64.b64decode(body, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc: raise InvalidRequest("The encoded request body is invalid.") from exc
        if body not in (None, ""):
            if not isinstance(body, str): raise InvalidRequest("The request body must be text.")
            try: body = json.loads(body)
            except json.JSONDecodeError as exc: raise InvalidRequest("The request body must contain valid JSON.") from exc
        path_parameters, query_parameters = event.get("pathParameters") or {}, event.get("queryStringParameters") or {}
        if not isinstance(path_parameters, dict) or not isinstance(query_parameters, dict): raise InvalidRequest("Request parameters must be objects.")
        return cls(method.upper(), path, resource, request_context["stage"], body, path_parameters, query_parameters)


class ApiResponse:
    """API Gateway REST API Lambda proxy JSON response."""
    def __init__(self, status_code, payload, allowed_methods): self.status_code, self.payload, self.allowed_methods = status_code, payload, allowed_methods
    def to_dict(self):
        response = {"statusCode": self.status_code, "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "null"), "Access-Control-Allow-Headers": "Content-Type,Accept", "Access-Control-Allow-Methods": self.allowed_methods}}
        if self.payload is not None: response["body"] = json.dumps(self.payload, separators=(",", ":"))
        return response


def get_table(table_name): return boto3.resource("dynamodb").Table(table_name)
def get_s3_client(): return boto3.client("s3")
def error_response(status_code, code, message): return ApiResponse(status_code, {"error": {"code": code, "message": message}}, ALLOWED_METHODS).to_dict()
def parse_positive_expiry(value):
    try: expiry = int(value)
    except (TypeError, ValueError): return None
    return expiry if expiry > 0 else None

def public_record(item): return {key: value for key, value in item.items() if key not in {"PK", "SK"}}


def lambda_handler(event, context):
    """List evidence newest first and add response-only short-lived download URLs."""
    try: request = ApiRequest.from_event(event)
    except InvalidRequest as exc: return error_response(400, "INVALID_REQUEST", str(exc))
    if request.stage != EXPECTED_STAGE: return error_response(400, "INVALID_REQUEST", "The request stage is not supported.")
    if request.method != EXPECTED_METHOD: return error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
    if request.path != EXPECTED_PATH or request.resource != EXPECTED_RESOURCE: return error_response(404, "NOT_FOUND", "Route not found.")
    if any(not os.environ.get(name) for name in REQUIRED_ENVIRONMENT): return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    expiry = parse_positive_expiry(os.environ["DOWNLOAD_URL_EXPIRY_SECONDS"])
    if expiry is None: return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    try:
        result = get_table(os.environ["TABLE_NAME"]).query(KeyConditionExpression=(Key("PK").eq(USER_KEY) & Key("SK").begins_with(EVIDENCE_SORT_KEY_PREFIX)), ScanIndexForward=False)
        s3 = get_s3_client()
        records = []
        for item in result.get("Items", []):
            asset_key = item.get("assetKey")
            if not isinstance(asset_key, str) or not asset_key.startswith(ASSET_KEY_PREFIX): raise ValueError("Invalid stored asset key")
            record = public_record(item)
            record["assetUrl"] = s3.generate_presigned_url("get_object", Params={"Bucket": os.environ["ASSET_BUCKET"], "Key": asset_key}, ExpiresIn=expiry)
            records.append(record)
    except Exception:
        return error_response(500, "DEPENDENCY_ERROR", "Evidence could not be listed.")
    return ApiResponse(200, {"items": records}, ALLOWED_METHODS).to_dict()
