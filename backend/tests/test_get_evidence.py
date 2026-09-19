import json
from pathlib import Path

from functions.get_evidence import lambda_function

EVENT_PATH = Path(__file__).parents[1] / "events" / "get_evidence_demo_id.json"


def load_event(): return json.loads(EVENT_PATH.read_text(encoding="utf-8"))


def configured(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("TABLE_NAME", "proofstack-test")
    monkeypatch.setenv("ASSET_BUCKET", "proofstack-private-test")
    monkeypatch.setenv("DOWNLOAD_URL_EXPIRY_SECONDS", "300")


def body(response): return json.loads(response["body"])


def test_gets_exact_record_and_generates_transient_get_url(monkeypatch, caplog):
    configured(monkeypatch); calls = []
    class FakeTable:
        def get_item(self, **kwargs):
            calls.append(("get", kwargs)); return {"Item": {"PK": "USER#demo", "SK": "EVIDENCE#demo-id", "id": "demo-id", "assetKey": "evidence/demo/demo-id/receipt.pdf"}}
    class FakeS3:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            calls.append(("sign", operation, Params, ExpiresIn)); return "https://temporary.example.test/download"
    monkeypatch.setattr(lambda_function, "get_table", lambda _: FakeTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FakeS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 200
    assert calls == [("get", {"Key": {"PK": "USER#demo", "SK": "EVIDENCE#demo-id"}}), ("sign", "get_object", {"Bucket": "proofstack-private-test", "Key": "evidence/demo/demo-id/receipt.pdf"}, 300)]
    record = body(response)
    assert record["assetUrl"] == "https://temporary.example.test/download" and "PK" not in record and "SK" not in record
    assert "https://temporary.example.test" not in caplog.text


def test_retains_not_found_and_rejects_invalid_route(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_table", lambda _: type("Table", (), {"get_item": lambda self, **kwargs: {}})())
    assert lambda_function.lambda_handler(load_event(), None)["statusCode"] == 404
    event = load_event(); event["pathParameters"]["id"] = "different"
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 404


def test_requires_phase_four_config_and_sanitizes_failures(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_table", lambda _: (_ for _ in ()).throw(AssertionError("DynamoDB called")))
    monkeypatch.delenv("DOWNLOAD_URL_EXPIRY_SECONDS")
    assert body(lambda_function.lambda_handler(load_event(), None))["error"]["code"] == "CONFIGURATION_ERROR"
    configured(monkeypatch)
    class GoodTable:
        def get_item(self, **kwargs): return {"Item": {"assetKey": "evidence/demo/demo-id/receipt.pdf"}}
    class BadS3:
        def generate_presigned_url(self, *args, **kwargs): raise RuntimeError("secret signing")
    monkeypatch.setattr(lambda_function, "get_table", lambda _: GoodTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: BadS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500 and body(response)["error"]["code"] == "DEPENDENCY_ERROR" and "secret" not in response["body"]


def test_declares_signed_download_contract():
    source = Path(lambda_function.__file__).read_text(encoding="utf-8")
    assert lambda_function.REQUIRED_ENVIRONMENT == ("ALLOWED_ORIGIN", "TABLE_NAME", "ASSET_BUCKET", "DOWNLOAD_URL_EXPIRY_SECONDS")
    assert "s3:GetObject" in source and "assetUrl" in source
