import json
from pathlib import Path

from functions.delete_evidence import lambda_function

EVENT_PATH = Path(__file__).parents[1] / "events" / "delete_evidence_demo_id.json"


def load_event(): return json.loads(EVENT_PATH.read_text(encoding="utf-8"))


def configured(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("TABLE_NAME", "proofstack-test")
    monkeypatch.setenv("ASSET_BUCKET", "proofstack-private-test")


def body(response): return json.loads(response["body"])


def test_deletes_exact_s3_object_before_metadata_and_returns_bodyless_204(monkeypatch):
    configured(monkeypatch); calls = []
    class FakeTable:
        def get_item(self, **kwargs): calls.append(("get", kwargs)); return {"Item": {"assetKey": "evidence/demo/demo-id/receipt.pdf"}}
        def delete_item(self, **kwargs): calls.append(("dynamo-delete", kwargs))
    class FakeS3:
        def delete_object(self, **kwargs): calls.append(("s3-delete", kwargs))
    monkeypatch.setattr(lambda_function, "get_table", lambda _: FakeTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FakeS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 204 and "body" not in response
    assert calls == [("get", {"Key": {"PK": "USER#demo", "SK": "EVIDENCE#demo-id"}}), ("s3-delete", {"Bucket": "proofstack-private-test", "Key": "evidence/demo/demo-id/receipt.pdf"}), ("dynamo-delete", {"Key": {"PK": "USER#demo", "SK": "EVIDENCE#demo-id"}})]


def test_retains_metadata_when_s3_delete_fails(monkeypatch):
    configured(monkeypatch); deleted = []
    class FakeTable:
        def get_item(self, **kwargs): return {"Item": {"assetKey": "evidence/demo/demo-id/receipt.pdf"}}
        def delete_item(self, **kwargs): deleted.append(kwargs)
    class BadS3:
        def delete_object(self, **kwargs): raise RuntimeError("secret object failure")
    monkeypatch.setattr(lambda_function, "get_table", lambda _: FakeTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: BadS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500 and body(response)["error"] == {"code": "DEPENDENCY_ERROR", "message": "Evidence could not be deleted."}
    assert not deleted and "secret" not in response["body"]


def test_returns_not_found_and_rejects_unsafe_stored_key_without_deleting(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_table", lambda _: type("Table", (), {"get_item": lambda self, **kwargs: {}})())
    assert lambda_function.lambda_handler(load_event(), None)["statusCode"] == 404
    calls = []
    class UnsafeTable:
        def get_item(self, **kwargs): return {"Item": {"assetKey": "other-user/private.pdf"}}
        def delete_item(self, **kwargs): calls.append("dynamo")
    monkeypatch.setattr(lambda_function, "get_table", lambda _: UnsafeTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: type("S3", (), {"delete_object": lambda self, **kwargs: calls.append("s3")})())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500 and not calls


def test_requires_bucket_and_keeps_standard_route_errors(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_table", lambda _: (_ for _ in ()).throw(AssertionError("DynamoDB called")))
    monkeypatch.delenv("ASSET_BUCKET")
    assert body(lambda_function.lambda_handler(load_event(), None))["error"]["code"] == "CONFIGURATION_ERROR"
    configured(monkeypatch)
    event = load_event(); event["httpMethod"] = "GET"
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 405


def test_declares_s3_first_least_privilege_contract():
    source = Path(lambda_function.__file__).read_text(encoding="utf-8")
    assert lambda_function.REQUIRED_ENVIRONMENT == ("ALLOWED_ORIGIN", "TABLE_NAME", "ASSET_BUCKET")
    assert "s3:DeleteObject" in source and "dynamodb:DeleteItem" in source
