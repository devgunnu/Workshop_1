"""API Gateway REST API handler for retrieving an evidence record.

IAM: dynamodb:GetItem on the exact table; s3:GetObject on
arn:aws:s3:::<asset-bucket>/evidence/demo/*.
"""

import base64
import binascii
import json
import os

import boto3

EXPECTED_METHOD = "GET"
EXPECTED_RESOURCE = "/evidence/{id}"
EXPECTED_STAGE = "prod"
PATH_PARAMETER = "id"
ALLOWED_METHODS = "GET,DELETE,OPTIONS"
REQUIRED_ENVIRONMENT = ("ALLOWED_ORIGIN", "TABLE_NAME", "ASSET_BUCKET", "DOWNLOAD_URL_EXPIRY_SECONDS")
USER_KEY = "USER#demo"
EVIDENCE_SORT_KEY_PREFIX = "EVIDENCE#"
ASSET_KEY_PREFIX = "evidence/demo/"


class InvalidRequest(ValueError): pass
class ApiRequest:
    """Normalized API Gateway REST API Lambda proxy request."""
    def __init__(self, method, path, resource, stage, body, path_parameters, query_parameters): self.method, self.path, self.resource, self.stage, self.body, self.path_parameters, self.query_parameters = method, path, resource, stage, body, path_parameters, query_parameters
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
def public_record(item): return {key: value for key, value in item.items() if key not in {"PK", "SK"}}
def error_response(status_code, code, message): return ApiResponse(status_code, {"error": {"code": code, "message": message}}, ALLOWED_METHODS).to_dict()
def parse_positive_expiry(value):
    try: expiry = int(value)
    except (TypeError, ValueError): return None
    return expiry if expiry > 0 else None

def lambda_handler(event, context):
    """Return one record with a response-only short-lived download URL."""
    try: request = ApiRequest.from_event(event)
    except InvalidRequest as exc: return error_response(400, "INVALID_REQUEST", str(exc))
    if request.stage != EXPECTED_STAGE: return error_response(400, "INVALID_REQUEST", "The request stage is not supported.")
    if request.method != EXPECTED_METHOD: return error_response(405, "METHOD_NOT_ALLOWED", "Method not allowed.")
    evidence_id = request.path_parameters.get(PATH_PARAMETER)
    if request.resource != EXPECTED_RESOURCE or not isinstance(evidence_id, str) or not evidence_id or request.path != "/evidence/" + evidence_id: return error_response(404, "NOT_FOUND", "Route not found.")
    if any(not os.environ.get(name) for name in REQUIRED_ENVIRONMENT): return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    expiry = parse_positive_expiry(os.environ["DOWNLOAD_URL_EXPIRY_SECONDS"])
    if expiry is None: return error_response(500, "CONFIGURATION_ERROR", "Required service configuration is missing.")
    try: result = get_table(os.environ["TABLE_NAME"]).get_item(Key={"PK": USER_KEY, "SK": EVIDENCE_SORT_KEY_PREFIX + evidence_id})
    except Exception: return error_response(500, "DEPENDENCY_ERROR", "Evidence could not be retrieved.")
    item = result.get("Item")
    if not item: return error_response(404, "NOT_FOUND", "Evidence not found.")
    asset_key = item.get("assetKey")
    if not isinstance(asset_key, str) or not asset_key.startswith(ASSET_KEY_PREFIX): return error_response(500, "DEPENDENCY_ERROR", "Evidence could not be retrieved.")
    try: asset_url = get_s3_client().generate_presigned_url("get_object", Params={"Bucket": os.environ["ASSET_BUCKET"], "Key": asset_key}, ExpiresIn=expiry)
    except Exception: return error_response(500, "DEPENDENCY_ERROR", "Evidence could not be retrieved.")
    record = public_record(item); record["assetUrl"] = asset_url
    return ApiResponse(200, record, ALLOWED_METHODS).to_dict()
