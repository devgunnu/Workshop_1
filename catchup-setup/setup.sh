#!/usr/bin/env bash
# ProofStack catch-up setup — for AWS SBG at ASU workshop members who are
# behind. Run this in AWS CloudShell, in your own AWS account.
#
# What it does:
#   1. Clones the workshop repo and pulls the five finished Lambda handlers
#      from backend/functions/*/lambda_function.py.
#   2. Zips each one standalone (matching the runbook: no layers, no shared
#      modules) and uploads the zips to a small deploy-only S3 bucket.
#   3. Deploys template.yaml, which recreates the exact Phase 1-4 end state
#      from the runbook: ProofStackApi (stage prod), five Lambdas with the
#      runbook's named inline IAM policies, ProofStackEvidence, and the
#      private evidence bucket. /workshop is not created — Phase 4's
#      checkpoint has it already removed.
#   4. Runs a real end-to-end smoke test (presign -> PUT -> create -> list
#      -> get -> delete) against the deployed API and leaves the table
#      empty afterward, the same state a live participant reaches at the
#      end of section 4.8.
#   5. Prints the worksheet values from the runbook's "Record AWS-generated
#      values as they appear" table.
#
# This is a scripted exception to the runbook's console-only rule, meant
# only to let someone who missed Phases 1-4 catch up before the room moves
# on to Phase 5 (frontend), IAM tightening, and Cognito. It is not a
# replacement for the Lambda Console test events in runbook sections 3.7
# and 4.8 — run those yourself against the deployed functions so you get
# the same hands-on checkpoint everyone else got.
#
# Safe to re-run: it reuses the deploy bucket and updates the stack in
# place if you run it again (e.g. after the repo gets new handler code).

set -euo pipefail

STACK_NAME="${STACK_NAME:-ProofStackCatchup}"
REPO_URL="${REPO_URL:-https://github.com/AWS-SBG-ASU/Workshop_1.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
ALLOWED_ORIGIN="${ALLOWED_ORIGIN:-http://localhost:5173}"
STATE_FILE="${STATE_FILE:-$HOME/.proofstack-catchup-state.json}"

FUNCTIONS=(create_evidence list_evidence get_evidence delete_evidence presign_upload)

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$1" >&2; }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------------
# 0. Preflight
# ---------------------------------------------------------------------
log "Checking prerequisites"
for bin in aws git zip python3; do
  command -v "$bin" >/dev/null 2>&1 || die "'$bin' is not available. This script expects to run in AWS CloudShell."
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)" \
  || die "Could not call AWS STS. Are you signed in?"
REGION="$(aws configure get region || true)"
if [[ -z "$REGION" ]]; then
  REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
fi
[[ -n "$REGION" ]] || die "Could not determine the current Region. Run 'aws configure set region <region>' first."

CODE_BUCKET="proofstack-deploy-${ACCOUNT_ID}-${REGION}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

echo "  Account:      $ACCOUNT_ID"
echo "  Region:       $REGION"
echo "  Stack name:   $STACK_NAME"
echo "  Deploy bucket: $CODE_BUCKET"

[[ -f "$SCRIPT_DIR/template.yaml" ]] || die "template.yaml not found next to setup.sh."

# ---------------------------------------------------------------------
# 1. Clone the repo and locate the five handlers
# ---------------------------------------------------------------------
log "Cloning $REPO_URL (branch $REPO_BRANCH)"
git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$WORK_DIR/repo" \
  || die "Could not clone the workshop repo. Check REPO_URL/REPO_BRANCH or your network."

BACKEND_DIR="$WORK_DIR/repo/backend/functions"
[[ -d "$BACKEND_DIR" ]] || die "backend/functions not found in the repo. Has it been restructured?"

missing=0
for fn in "${FUNCTIONS[@]}"; do
  f="$BACKEND_DIR/$fn/lambda_function.py"
  if [[ ! -f "$f" ]]; then
    warn "Missing $f"
    missing=1
  fi
done
if [[ "$missing" -eq 1 ]]; then
  die "One or more handlers are not in the repo yet (Prompt 1/2 may not be merged). Ask in the club channel before re-running."
fi

# ---------------------------------------------------------------------
# 2. Zip each handler standalone (single file, no shared modules/layers)
# ---------------------------------------------------------------------
log "Packaging Lambda zips"
ZIP_DIR="$WORK_DIR/zips"
mkdir -p "$ZIP_DIR"
for fn in "${FUNCTIONS[@]}"; do
  src="$BACKEND_DIR/$fn/lambda_function.py"
  out="$ZIP_DIR/${fn}.zip"
  ( cd "$(dirname "$src")" && zip -q -j "$out" "$(basename "$src")" )
  echo "  $fn -> $(basename "$out")"
done

# ---------------------------------------------------------------------
# 3. Create the deploy bucket (idempotent) and upload
# ---------------------------------------------------------------------
log "Ensuring deploy bucket exists"
if ! aws s3api head-bucket --bucket "$CODE_BUCKET" 2>/dev/null; then
  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket --bucket "$CODE_BUCKET" --region "$REGION" >/dev/null
  else
    aws s3api create-bucket --bucket "$CODE_BUCKET" --region "$REGION" \
      --create-bucket-configuration LocationConstraint="$REGION" >/dev/null
  fi
  aws s3api put-public-access-block --bucket "$CODE_BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
  echo "  Created $CODE_BUCKET"
else
  echo "  Reusing $CODE_BUCKET"
fi

log "Uploading Lambda zips"
aws s3 cp "$ZIP_DIR" "s3://$CODE_BUCKET/lambda-code/" --recursive --only-show-errors

# ---------------------------------------------------------------------
# 4. Deploy the stack
# ---------------------------------------------------------------------
log "Deploying CloudFormation stack ($STACK_NAME)"
aws cloudformation deploy \
  --stack-name "$STACK_NAME" \
  --template-file "$SCRIPT_DIR/template.yaml" \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      CodeBucketName="$CODE_BUCKET" \
      AllowedOrigin="$ALLOWED_ORIGIN" \
  --no-fail-on-empty-changeset

OUTPUTS_JSON="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" --query 'Stacks[0].Outputs' --output json)"

get_output() {
  python3 - "$1" <<'PY'
import json, sys
key = sys.argv[1]
outputs = json.load(sys.stdin)
for o in outputs:
    if o["OutputKey"] == key:
        print(o["OutputValue"])
        break
PY
}

API_ID="$(echo "$OUTPUTS_JSON" | get_output ApiId)"
INVOKE_BASE="$(echo "$OUTPUTS_JSON" | get_output InvokeBaseUrl)"
TABLE_ARN="$(echo "$OUTPUTS_JSON" | get_output TableArn)"
ASSET_BUCKET="$(echo "$OUTPUTS_JSON" | get_output AssetBucketName)"
ASSET_OBJECT_ARN="$(echo "$OUTPUTS_JSON" | get_output AssetBucketObjectArnPattern)"

cat > "$STATE_FILE" <<EOF
{
  "stackName": "$STACK_NAME",
  "region": "$REGION",
  "accountId": "$ACCOUNT_ID",
  "codeBucket": "$CODE_BUCKET",
  "assetBucket": "$ASSET_BUCKET",
  "invokeBase": "$INVOKE_BASE"
}
EOF

# ---------------------------------------------------------------------
# 5. Smoke test: presign -> PUT -> create -> list -> get -> delete
# ---------------------------------------------------------------------
log "Running end-to-end smoke test against $INVOKE_BASE"

py_json_get() {
  # py_json_get '<json>' key   -> prints value, or nothing if absent
  python3 - "$1" "$2" <<'PY'
import json, sys
data = json.loads(sys.argv[1])
key = sys.argv[2]
val = data.get(key, "")
print(val if val is not None else "")
PY
}

smoke_failed=0

presign_body='{"fileName":"catchup-smoke-test.txt","contentType":"text/plain"}'
presign_resp="$(curl -sS -o /tmp/presign.json -w '%{http_code}' -X POST "$INVOKE_BASE/uploads/presign" \
  -H 'Content-Type: application/json' -d "$presign_body" || echo "000")"
presign_json="$(cat /tmp/presign.json 2>/dev/null || echo '{}')"

if [[ "$presign_resp" != "200" ]]; then
  warn "POST /uploads/presign returned HTTP $presign_resp (expected 200). Body: $presign_json"
  smoke_failed=1
else
  UPLOAD_URL="$(py_json_get "$presign_json" uploadUrl)"
  ASSET_KEY="$(py_json_get "$presign_json" assetKey)"
  EXPIRES_IN="$(py_json_get "$presign_json" expiresIn)"
  echo "  presign OK -> assetKey=$ASSET_KEY expiresIn=$EXPIRES_IN"

  if [[ -n "$UPLOAD_URL" ]]; then
    put_resp="$(curl -sS -o /dev/null -w '%{http_code}' -X PUT "$UPLOAD_URL" \
      -H 'Content-Type: text/plain' --data-binary 'catchup smoke test file' || echo "000")"
    if [[ "$put_resp" != "200" ]]; then
      warn "Direct PUT to the presigned URL returned HTTP $put_resp (expected 200). Check bucket CORS and the presign handler's Content-Type handling."
      smoke_failed=1
    else
      echo "  direct PUT to private bucket OK"
    fi
  else
    warn "presign response had no uploadUrl"
    smoke_failed=1
  fi
fi

if [[ "$smoke_failed" -eq 0 && -n "${ASSET_KEY:-}" ]]; then
  create_body="$(python3 - "$ASSET_KEY" <<'PY'
import json, sys
asset_key = sys.argv[1]
print(json.dumps({
    "title": "Catch-up smoke test",
    "description": "Created by setup.sh to verify the deployed stack end-to-end.",
    "tags": ["catchup", "smoke-test"],
    "fileName": "catchup-smoke-test.txt",
    "contentType": "text/plain",
    "assetKey": asset_key,
}))
PY
)"
  create_resp="$(curl -sS -o /tmp/create.json -w '%{http_code}' -X POST "$INVOKE_BASE/evidence" \
    -H 'Content-Type: application/json' -d "$create_body" || echo "000")"
  create_json="$(cat /tmp/create.json 2>/dev/null || echo '{}')"

  if [[ "$create_resp" != "201" ]]; then
    warn "POST /evidence returned HTTP $create_resp (expected 201). Body: $create_json"
    smoke_failed=1
  else
    RECORD_ID="$(py_json_get "$create_json" id)"
    echo "  create OK -> id=$RECORD_ID"

    get_resp="$(curl -sS -o /tmp/get.json -w '%{http_code}' "$INVOKE_BASE/evidence/$RECORD_ID" || echo "000")"
    if [[ "$get_resp" == "200" ]]; then
      echo "  get OK"
    else
      warn "GET /evidence/$RECORD_ID returned HTTP $get_resp (expected 200)"
      smoke_failed=1
    fi

    list_resp="$(curl -sS -o /tmp/list.json -w '%{http_code}' "$INVOKE_BASE/evidence" || echo "000")"
    if [[ "$list_resp" == "200" ]] && grep -q "$RECORD_ID" /tmp/list.json; then
      echo "  list OK (record present, newest first expected)"
    else
      warn "GET /evidence did not include the new record as expected (HTTP $list_resp)"
      smoke_failed=1
    fi

    delete_resp="$(curl -sS -o /dev/null -w '%{http_code}' -X DELETE "$INVOKE_BASE/evidence/$RECORD_ID" || echo "000")"
    if [[ "$delete_resp" == "204" ]]; then
      echo "  delete OK (204, S3 object then DynamoDB item removed)"
    else
      warn "DELETE /evidence/$RECORD_ID returned HTTP $delete_resp (expected 204)"
      smoke_failed=1
    fi

    final_get_resp="$(curl -sS -o /dev/null -w '%{http_code}' "$INVOKE_BASE/evidence/$RECORD_ID" || echo "000")"
    if [[ "$final_get_resp" == "404" ]]; then
      echo "  post-delete GET correctly 404s"
    else
      warn "Record still retrievable after delete (HTTP $final_get_resp)"
    fi
  fi
fi

workshop_resp="$(curl -sS -o /dev/null -w '%{http_code}' "$INVOKE_BASE/workshop" || echo "000")"
if [[ "$workshop_resp" == "403" || "$workshop_resp" == "404" ]]; then
  echo "  /workshop correctly absent (HTTP $workshop_resp)"
else
  warn "/workshop returned HTTP $workshop_resp; expected it to be absent (403/404) at the Phase 4 checkpoint"
fi

# ---------------------------------------------------------------------
# 6. Worksheet
# ---------------------------------------------------------------------
log "Worksheet values (runbook: 'Record AWS-generated values as they appear')"
cat <<EOF

  AWS account ID     : $ACCOUNT_ID
  Region             : $REGION
  REST API ID        : $API_ID
  Invoke base        : $INVOKE_BASE
  DynamoDB table ARN : $TABLE_ARN
  Private object ARN : $ASSET_OBJECT_ARN
  Private bucket name: $ASSET_BUCKET

EOF

if [[ "$smoke_failed" -eq 1 ]]; then
  warn "One or more smoke-test steps failed above. The stack is deployed, but check the runbook's Troubleshooting section before continuing."
  exit 2
fi

log "Done. You are caught up through the Phase 4 checkpoint."
cat <<'EOF'
Still worth doing by hand, so you get the same hands-on checkpoint as
everyone else:
  - The Lambda Console test events from runbook sections 3.7 and 4.8
    (open each function in the Lambda console and run its event).
  - Everything from Chapter 5 onward: building/hosting the frontend,
    swapping ALLOWED_ORIGIN and CORS from localhost to the website
    origin, then the room's IAM-tightening and Cognito setup.
EOF
