import json
from pathlib import Path

from functions.list_evidence import lambda_function

EVENT_PATH = Path(__file__).parents[1] / "events" / "get_evidence.json"


def load_event(): return json.loads(EVENT_PATH.read_text(encoding="utf-8"))


def configured(monkeypatch):
    monkeypatch.setenv("ALLOWED_ORIGIN", "https://app.example.com")
    monkeypatch.setenv("TABLE_NAME", "proofstack-test")
    monkeypatch.setenv("ASSET_BUCKET", "proofstack-private-test")
    monkeypatch.setenv("DOWNLOAD_URL_EXPIRY_SECONDS", "300")


def body(response): return json.loads(response["body"])


def items():
    return [
        {"PK": "USER#demo", "SK": "EVIDENCE#20250103T000000000000Z-new", "id": "new", "title": "New", "assetKey": "evidence/demo/new/new.pdf"},
        {"PK": "USER#demo", "SK": "EVIDENCE#20250102T000000000000Z-old", "id": "old", "title": "Old", "assetKey": "evidence/demo/old/old.pdf"},
    ]


def test_lists_newest_first_with_transient_signed_urls(monkeypatch, caplog):
    configured(monkeypatch); query_calls = []; signing_calls = []
    class FakeTable:
        def query(self, **kwargs): query_calls.append(kwargs); return {"Items": items(), "LastEvaluatedKey": {"PK": "ignored"}}
    class FakeS3:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            signing_calls.append((operation, Params, ExpiresIn)); return "https://temporary.example.test/" + Params["Key"]
    monkeypatch.setattr(lambda_function, "get_table", lambda _: FakeTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: FakeS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 200
    assert query_calls[0]["ScanIndexForward"] is False
    records = body(response)["items"]
    assert [record["id"] for record in records] == ["new", "old"]
    assert all("PK" not in record and "SK" not in record and record["assetUrl"].startswith("https://temporary.example.test/") for record in records)
    assert signing_calls == [("get_object", {"Bucket": "proofstack-private-test", "Key": item["assetKey"]}, 300) for item in items()]
    assert "nextToken" not in body(response) and "https://temporary.example.test" not in caplog.text


def test_preserves_query_not_scan_contract():
    source = Path(lambda_function.__file__).read_text(encoding="utf-8")
    assert lambda_function.REQUIRED_ENVIRONMENT == ("ALLOWED_ORIGIN", "TABLE_NAME", "ASSET_BUCKET", "DOWNLOAD_URL_EXPIRY_SECONDS")
    assert "ScanIndexForward=False" in source and ".scan(" not in source
    assert "s3:GetObject" in source and "assetUrl" not in source.split("def public_record", 1)[1].split("def ", 1)[0]


def test_rejects_bad_route_or_missing_invalid_configuration_before_aws(monkeypatch):
    configured(monkeypatch)
    monkeypatch.setattr(lambda_function, "get_table", lambda _: (_ for _ in ()).throw(AssertionError("DynamoDB called")))
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: (_ for _ in ()).throw(AssertionError("S3 called")))
    event = load_event(); event["httpMethod"] = "POST"
    assert lambda_function.lambda_handler(event, None)["statusCode"] == 405
    monkeypatch.delenv("ASSET_BUCKET")
    assert body(lambda_function.lambda_handler(load_event(), None))["error"]["code"] == "CONFIGURATION_ERROR"
    configured(monkeypatch); monkeypatch.setenv("DOWNLOAD_URL_EXPIRY_SECONDS", "0")
    assert body(lambda_function.lambda_handler(load_event(), None))["error"]["code"] == "CONFIGURATION_ERROR"


def test_sanitizes_dynamodb_and_s3_failures(monkeypatch):
    configured(monkeypatch)
    class BadTable:
        def query(self, **kwargs): raise RuntimeError("secret query")
    monkeypatch.setattr(lambda_function, "get_table", lambda _: BadTable())
    response = lambda_function.lambda_handler(load_event(), None)
    assert body(response)["error"] == {"code": "DEPENDENCY_ERROR", "message": "Evidence could not be listed."}
    class GoodTable:
        def query(self, **kwargs): return {"Items": items()}
    class BadS3:
        def generate_presigned_url(self, *args, **kwargs): raise RuntimeError("secret URL")
    monkeypatch.setattr(lambda_function, "get_table", lambda _: GoodTable())
    monkeypatch.setattr(lambda_function, "get_s3_client", lambda: BadS3())
    response = lambda_function.lambda_handler(load_event(), None)
    assert response["statusCode"] == 500 and "secret" not in response["body"]
