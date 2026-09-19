"""API Gateway REST API handler for creating private S3 upload URLs.

IAM: s3:PutObject on arn:aws:s3:::<asset-bucket>/evidence/demo/*.
"""

import base64
import binascii
import json
import os
import re
import uuid

import boto3

EXPECTED_METHOD = "POST"
EXPECTED_PATH = "/uploads/presign"
EXPECTED_RESOURCE = "/uploads/presign"
EXPECTED_STAGE = "prod"
ALLOWED_METHODS = "POST,OPTIONS"
REQUIRED_ENVIRONMENT = ("ALLOWED_ORIGIN", "ASSET_BUCKET", "UPLOAD_URL_EXPIRY_SECONDS")
ASSET_KEY_PREFIX = "evidence/demo/"


class InvalidRequest(ValueError):
    """Raised when a REST API Lambda proxy event cannot be parsed safely."""


class ApiRequest:
    """Normalized API Gateway REST API Lambda proxy request."""

    def __init__(self, method, path, resource, stage, body, path_parameters, query_parameters):
        self.method = method
        self.path = path
        self.resource = resource
        self.stage = stage
        self.body = body
        self.path_parameters = path_parameters
        self.query_parameters = query_parameters

    @classmethod
    def from_event(cls, event):
        if not isinstance(event, dict):
            raise InvalidRequest("Expected an API Gateway REST API Lambda proxy event.")
        method, path, resource, request_context = (event.get("httpMethod"), event.get("path"), event.get("resource"), event.get("requestContext"))
        if not isinstance(method, str) or not method.strip():
            raise InvalidRequest("The HTTP method is missing.")
        if not isinstance(path, str) or not path.startswith("/"):
            raise InvalidRequest("The request path is missing.")
        if not isinstance(resource, str) or not resource.startswith("/"):
            raise InvalidRequest("The request resource is missing.")
        if not isinstance(request_context, dict) or not isinstance(request_context.get("stage"), str) or not request_context["stage"].strip():
            raise InvalidRequest("The request stage is missing.")
        body = event.get("body")
        if event.get("isBase64Encoded", False) and body is not None:
            if not isinstance(body, str):
                raise InvalidRequest("The encoded request body must be text.")
            try:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise InvalidRequest("The encoded request body is invalid.") from exc
        if body in (None, ""):
            parsed_body = None
        elif not isinstance(body, str):
            raise InvalidRequest("The request body must be text.")
        else:
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise InvalidRequest("The request body must contain valid JSON.") from exc
        path_parameters, query_parameters = event.get("pathParameters") or {}, event.get("queryStringParameters") or {}
        if not isinstance(path_parameters, dict) or not isinstance(query_parameters, dict):
            raise InvalidRequest("Request parameters must be objects.")
        return cls(method.upper(), path, resource, request_context["stage"], parsed_body, path_parameters, query_parameters)


class ApiResponse:
    """API Gateway REST API Lambda proxy JSON response."""

    def __init__(self, status_code, payload, allowed_methods):
        self.status_code, self.payload, self.allowed_methods = status_code, payload, allowed_methods

    def to_dict(self):
        response = {"statusCode": self.status_code, "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "null"), "Access-Control-Allow-Headers": "Content-Type,Accept", "Access-Control-Allow-Methods": self.allowed_methods}}
        if self.payload is not None:
            response["body"] = json.dumps(self.payload, separators=(",", ":"))
        return response


def error_response(status_code, code, message):
    return ApiResponse(status_code, {"error": {"code": code, "message": message}}, ALLOWED_METHODS).to_dict()


def get_s3_client():
    """Return a replaceable S3 client seam for local tests."""
    return boto3.client("s3", region_name="us-east-2")


def parse_positive_expiry(value):
    try:
        expiry = int(value)
    except (TypeError, ValueError):
        return None
    return expiry if expiry > 0 else None


def sanitize_filename(file_name):
    name = file_name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._")
    return name or "upload"


def lambda_handler(event, context):
    """Return a content-type-bound, short-lived private upload URL."""
    try:
        request = ApiRequest.from_event(event)
    except InvalidRequest as exc:
        return error_response(400, "INVALID_REQUEST", str(exc))
    if request.stage != EXPECTED_STAGE:
        return error_response(400, "INVALID_REQUEST", "The request stage is not supported.")
    if request.method != EXPECTED_METHOD:
        return error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
    if request.path != EXPECTED_PATH or request.resource != EXPECTED_RESOURCE:
        return error_response(404, "NOT_FOUND", "Route not found.")
    if any(not os.environ.get(name) for name in REQUIRED_ENVIRONMENT):
        return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    expiry = parse_positive_expiry(os.environ["UPLOAD_URL_EXPIRY_SECONDS"])
    if expiry is None:
        return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    if not isinstance(request.body, dict):
        return error_response(400, "VALIDATION_ERROR", "fileName and contentType are required.")
    file_name, content_type = request.body.get("fileName"), request.body.get("contentType")
    if not isinstance(file_name, str) or not file_name.strip() or not isinstance(content_type, str) or not content_type.strip():
        return error_response(400, "VALIDATION_ERROR", "fileName and contentType are required.")
    asset_key = ASSET_KEY_PREFIX + uuid.uuid4().hex + "-" + sanitize_filename(file_name.strip())
    try:
        upload_url = get_s3_client().generate_presigned_url("put_object", Params={"Bucket": os.environ["ASSET_BUCKET"], "Key": asset_key, "ContentType": content_type.strip()}, ExpiresIn=expiry)
    except Exception:
        return error_response(500, "DEPENDENCY_ERROR", "Upload URL could not be created.")
    return ApiResponse(200, {"uploadUrl": upload_url, "assetKey": asset_key, "expiresIn": expiry}, ALLOWED_METHODS).to_dict()
