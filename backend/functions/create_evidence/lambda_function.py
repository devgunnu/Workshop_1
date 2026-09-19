"""API Gateway REST API handler for creating an evidence metadata record."""

import base64
import binascii
import json
import os
import re
import uuid
from datetime import datetime, timezone

import boto3

EXPECTED_METHOD = "POST"
EXPECTED_PATH = "/evidence"
EXPECTED_RESOURCE = "/evidence"
EXPECTED_STAGE = "prod"
ALLOWED_METHODS = "GET,POST,OPTIONS"
REQUIRED_ENVIRONMENT = ("ALLOWED_ORIGIN", "TABLE_NAME")
USER_KEY = "USER#demo"
EVIDENCE_SORT_KEY_PREFIX = "EVIDENCE#"
ASSET_KEY_PREFIX = "evidence/demo/"
CONTENT_TYPE_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")


class InvalidRequest(ValueError):
    """Raised when a REST API Lambda proxy event cannot be parsed safely."""


class ValidationError(ValueError):
    """Raised when submitted evidence metadata is invalid."""


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
        """Normalize a REST API Lambda proxy event."""
        if not isinstance(event, dict):
            raise InvalidRequest("Expected an API Gateway REST API Lambda proxy event.")

        method = event.get("httpMethod")
        path = event.get("path")
        resource = event.get("resource")
        request_context = event.get("requestContext")
        if not isinstance(method, str) or not method.strip():
            raise InvalidRequest("The HTTP method is missing.")
        if not isinstance(path, str) or not path.startswith("/"):
            raise InvalidRequest("The request path is missing.")
        if not isinstance(resource, str) or not resource.startswith("/"):
            raise InvalidRequest("The request resource is missing.")
        if not isinstance(request_context, dict):
            raise InvalidRequest("The request context is missing.")

        stage = request_context.get("stage")
        if not isinstance(stage, str) or not stage.strip():
            raise InvalidRequest("The request stage is missing.")

        body = event.get("body")
        if event.get("isBase64Encoded", False) and body is not None:
            if not isinstance(body, str):
                raise InvalidRequest("The encoded request body must be text.")
            try:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise InvalidRequest("The encoded request body is invalid.") from exc

        parsed_body = None
        if body not in (None, ""):
            if not isinstance(body, str):
                raise InvalidRequest("The request body must be text.")
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise InvalidRequest("The request body must contain valid JSON.") from exc

        path_parameters = event.get("pathParameters") or {}
        query_parameters = event.get("queryStringParameters") or {}
        if not isinstance(path_parameters, dict) or not isinstance(query_parameters, dict):
            raise InvalidRequest("Request parameters must be objects.")

        return cls(method.upper(), path, resource, stage, parsed_body, path_parameters, query_parameters)


class ApiResponse:
    """API Gateway REST API Lambda proxy JSON response."""

    def __init__(self, status_code, payload, allowed_methods):
        self.status_code = status_code
        self.payload = payload
        self.allowed_methods = allowed_methods

    def to_dict(self):
        """Render a Lambda proxy response with JSON and CORS headers."""
        response = {"statusCode": self.status_code, "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": os.environ.get("ALLOWED_ORIGIN", "null"), "Access-Control-Allow-Headers": "Content-Type,Accept", "Access-Control-Allow-Methods": self.allowed_methods}}
        if self.payload is not None:
            response["body"] = json.dumps(self.payload, separators=(",", ":"))
        return response


def get_table(table_name):
    """Return a replaceable DynamoDB table seam for local tests."""
    return boto3.resource("dynamodb").Table(table_name)


def current_utc_time():
    """Return the current time in UTC."""
    return datetime.now(timezone.utc)


def new_uuid_segment():
    """Return the compact random suffix for a new evidence ID."""
    return uuid.uuid4().hex[:8]


def error_response(status_code, code, message):
    """Build a standard ProofStack error response."""
    return ApiResponse(status_code, {"error": {"code": code, "message": message}}, ALLOWED_METHODS).to_dict()


def normalized_text(value, field, required=True):
    """Validate and trim an optional or required metadata text field."""
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string." % field)
    return value.strip()


def public_record(item):
    """Remove DynamoDB implementation keys from a response record."""
    return {key: value for key, value in item.items() if key not in {"PK", "SK"}}


def validate_metadata(body):
    """Validate incoming metadata before any DynamoDB access."""
    if not isinstance(body, dict):
        raise ValidationError("The request body must be a JSON object.")

    title = normalized_text(body.get("title"), "title")
    description = normalized_text(body.get("description"), "description", required=False)
    tags = body.get("tags")
    if not isinstance(tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValidationError("tags must be an array of non-empty strings.")

    file_name = normalized_text(body.get("fileName"), "fileName")
    content_type = normalized_text(body.get("contentType"), "contentType")
    if not CONTENT_TYPE_PATTERN.fullmatch(content_type):
        raise ValidationError("contentType must be a valid MIME type.")

    asset_key = normalized_text(body.get("assetKey"), "assetKey")
    if not asset_key.startswith(ASSET_KEY_PREFIX):
        raise ValidationError("assetKey must begin with evidence/demo/.")

    metadata = {"title": title, "tags": [tag.strip() for tag in tags], "fileName": file_name, "contentType": content_type, "assetKey": asset_key}
    if description is not None:
        metadata["description"] = description
    return metadata


def lambda_handler(event, context):
    """Create one evidence metadata record for the fixed demo user."""
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

    try:
        metadata = validate_metadata(request.body)
    except ValidationError as exc:
        return error_response(400, "VALIDATION_ERROR", str(exc))

    now = current_utc_time().astimezone(timezone.utc)
    evidence_id = now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + new_uuid_segment()
    record = {"id": evidence_id, **metadata, "createdAt": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}

    try:
        get_table(os.environ["TABLE_NAME"]).put_item(Item={"PK": USER_KEY, "SK": EVIDENCE_SORT_KEY_PREFIX + evidence_id, **record})
    except Exception:
        return error_response(500, "DEPENDENCY_ERROR", "Evidence could not be saved.")

    return ApiResponse(201, public_record(record), ALLOWED_METHODS).to_dict()
