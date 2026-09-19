import base64
import json
import re
from pathlib import Path

from functions.presign_upload import lambda_function

EVENT_PATH = Path(__file__).parents[1] / "events" / "post_uploads_presign.json"


def load_event():
    return json.loads(EVENT_PATH.read_text(encoding="utf-8"))


def configured(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("ASSET_BUCKET", "proofstack-private-test")
    monkeypatch.setenv("UPLOAD_URL_EXPIRY_SECONDS", "300")


def body(response):
    return json.loads(response["body"])


def test_parses_final_rest_proxy_event_and_base64_body():
    event = load_event()
    request = lambda_function.ApiRequest.from_event(event)
    assert (request.method, request.path, request.resource, request.stage) == ("POST", "/uploads/presign", "/uploads/presign", "prod")
    assert request.body == {"fileName": "receipt.pdf", "contentType": "application/pdf"}
    event["body"] = base64.b64encode(event["body"].encode()).decode()
    event["isBase64Encoded"] = True
    assert lambda_function.ApiRequest.from_event(event).body["fileName"] == "receipt.pdf"


def test_constructs_s3_client_for_the_evidence_bucket_region(monkeypatch):
    client = object()
    calls = []

    def fake_client(*args, **kwargs):
        calls.append((args, kwargs))
        return client

    monkeypatch.setattr(lambda_function.boto3, "client", fake_client)

    assert lambda_function.get_s3_client() is client
    assert calls == [(('s3',), {'region_name': 'us-east-2'})]


def test_presigns_content_type_bound_put_with_safe_unique_key(monkeypatch):
    configured(monkeypatch)
    calls = []

    class FakeS3:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            calls.append((operation, Params, ExpiresIn))
            return "https://signed.example.test/upload"

    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FakeS3())
    event = load_event()
    event["body"] = json.dumps({"fileName": "../Receipt final!.pdf", "contentType": "application/pdf"})
    response = lambda_function.lambda_handler(event, None)

    assert response["statusCode"] == 200
    result = body(response)
    assert result["uploadUrl"] == "https://signed.example.test/upload"
    assert result["expiresIn"] == 300 and isinstance(result["expiresIn"], int)
    assert result["assetKey"].startswith("evidence/demo/")
    assert "/" not in result["assetKey"].removeprefix("evidence/demo/")
    assert re.fullmatch(r"evidence/demo/[0-9a-f]+-[A-Za-z0-9._-]+", result["assetKey"])
    assert calls == [("put_object", {"Bucket": "proofstack-private-test", "Key": result["assetKey"], "ContentType": "application/pdf"}, 300)]


def test_generates_unique_keys_without_persisting_or_logging_urls(monkeypatch, caplog):
    configured(monkeypatch)

    class FakeS3:
        def generate_presigned_url(self, *args, **kwargs):
            return "https://secret.example.test/signed"

    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FakeS3())
    first = body(lambda_function.lambda_handler(load_event(), None))
    second = body(lambda_function.lambda_handler(load_event(), None))
    assert first["assetKey"] != second["assetKey"]
    assert "https://secret.example.test/signed" not in caplog.text
    assert "uploadUrl" not in Path(lambda_function.__file__).read_text(encoding="utf-8").split("generate_presigned_url", 1)[0]


def test_rejects_invalid_body_fields_and_expiry_before_aws_access(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: (_ for _ in ()).throw(AssertionError("S3 called")))
    for payload in ({}, {"fileName": " ", "contentType": "application/pdf"}, {"fileName": "receipt.pdf", "contentType": " "}, {"fileName": "receipt.pdf", "contentType": 1}):
        event = load_event(); event["body"] = json.dumps(payload)
        response = lambda_function.lambda_handler(event, None)
        assert response["statusCode"] == 400 and body(response)["error"]["code"] == "VALIDATION_ERROR"
    monkeypatch.setenv("UPLOAD_URL_EXPIRY_SECONDS", "zero")
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500 and body(response)["error"]["code"] == "CONFIGURATION_ERROR"


def test_reports_route_json_configuration_and_s3_failures_safely(monkeypatch):
    event = load_event(); event["body"] = "{"
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 400
    configured(monkeypatch)
    monkeypatch.delenv("ASSET_BUCKET")
    assert body(lambda_function.lambda_handler(load_event(), None))["error"]["code"] == "CONFIGURATION_ERROR"
    configured(monkeypatch)

    class FailingS3:
        def generate_presigned_url(self, *args, **kwargs):
            raise RuntimeError("private signing detail")

    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FailingS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500
    assert body(response)["error"] == {"code": "DEPENDENCY_ERROR", "message": "Upload URL could not be created."}
    assert "private signing detail" not in response["body"]
