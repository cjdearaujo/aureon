#!/usr/bin/env bash
# One-shot GCP deployment. Run it in Google Cloud Shell (gcloud is preinstalled and logged in):
#   PROJECT_ID=my-project ./deploy/deploy.sh
# Re-running it is safe: it updates existing resources.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-us-central1}"            # has Cloud Run L4 GPUs
REPO="agentic-trader"
APP="aureon"
FINR1="fin-r1"
SA="aureon-sa"
SA_EMAIL="$SA@$PROJECT_ID.iam.gserviceaccount.com"
IMG="$REGION-docker.pkg.dev/$PROJECT_ID/$REPO"
DEPLOY_FINR1="${DEPLOY_FINR1:-true}"
ADMIN_USERS="${ADMIN_USERS:-admin}"         # master accounts, comma-separated
USE_VERTEX="${USE_VERTEX:-false}"           # true = Gemini via Gemini Enterprise Agent Platform (ex-Vertex AI) (uses GCP credits, no API key)

gcloud config set project "$PROJECT_ID" >/dev/null
echo "==> Enabling APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com monitoring.googleapis.com aiplatform.googleapis.com >/dev/null

echo "==> Artifact Registry + service account"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"
gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1 || \
  gcloud iam service-accounts create "$SA" --display-name "Aureon runtime"

echo "==> Secrets (you'll be prompted for any that don't exist yet)"
put_secret() {  # name, prompt
  if ! gcloud secrets describe "$1" >/dev/null 2>&1; then
    read -r -s -p "$2: " val; echo
    printf '%s' "$val" | gcloud secrets create "$1" --data-file=- --replication-policy automatic >/dev/null
  fi
  gcloud secrets add-iam-policy-binding "$1" --member "serviceAccount:$SA_EMAIL" \
    --role roles/secretmanager.secretAccessor >/dev/null
}
if [[ "$USE_VERTEX" == "true" ]]; then
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA_EMAIL" \
    --role roles/aiplatform.user --condition None >/dev/null
  GEMINI_ENV="@GEMINI_VERTEX=true@GCP_PROJECT=$PROJECT_ID@GCP_LOCATION=global"; GEMINI_SECRET=""
else
  put_secret GOOGLE_API_KEY "Gemini API key (AI Studio)"
  GEMINI_ENV=""; GEMINI_SECRET="GOOGLE_API_KEY=GOOGLE_API_KEY:latest,"
fi
put_secret LANGSMITH_API_KEY "LangSmith API key"
put_secret DATABASE_URL      "Neon Postgres connection string"
put_secret APP_PASSCODE      "Client access code (share with your users)"
put_secret ADMIN_PASSCODE    "Admin access code (only for master accounts)"
if ! gcloud secrets describe SECRET_KEY >/dev/null 2>&1; then
  openssl rand -hex 32 | tr -d '\n' | gcloud secrets create SECRET_KEY --data-file=- >/dev/null
fi
gcloud secrets add-iam-policy-binding SECRET_KEY --member "serviceAccount:$SA_EMAIL" \
  --role roles/secretmanager.secretAccessor >/dev/null
if ! gcloud secrets describe TICK_TOKEN >/dev/null 2>&1; then
  openssl rand -hex 16 | tr -d '\n' | gcloud secrets create TICK_TOKEN --data-file=- >/dev/null
fi
gcloud secrets add-iam-policy-binding TICK_TOKEN --member "serviceAccount:$SA_EMAIL" \
  --role roles/secretmanager.secretAccessor >/dev/null

FINR1_URL=""
if [[ "$DEPLOY_FINR1" == "true" ]]; then
  echo "==> Building Fin-R1 GPU image (~10 min, bakes 4.7 GB of weights)"
  gcloud builds submit finr1 --tag "$IMG/$FINR1:latest" --machine-type e2-highcpu-8 --timeout 3600
  echo "==> Deploying Fin-R1 on an L4 GPU (scales to zero, private)"
  gcloud run deploy "$FINR1" --image "$IMG/$FINR1:latest" --region "$REGION" \
    --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 4 --memory 16Gi \
    --min-instances 0 --max-instances 1 --concurrency 4 --timeout 600 --no-cpu-throttling \
    --no-allow-unauthenticated --port 8080
  gcloud run services add-iam-policy-binding "$FINR1" --region "$REGION" \
    --member "serviceAccount:$SA_EMAIL" --role roles/run.invoker >/dev/null
  FINR1_URL=$(gcloud run services describe "$FINR1" --region "$REGION" --format 'value(status.url)')
fi

echo "==> Building main image (FinBERT + Whisper baked in)"
gcloud builds submit . --tag "$IMG/$APP:latest" --machine-type e2-highcpu-8 --timeout 3600

echo "==> Deploying main service"
gcloud run deploy "$APP" --image "$IMG/$APP:latest" --region "$REGION" --service-account "$SA_EMAIL" \
  --cpu 2 --memory 4Gi --min-instances 1 --max-instances 1 --concurrency 40 --timeout 3600 \
  --cpu-boost --session-affinity --allow-unauthenticated \
  --set-env-vars "^@^LANGSMITH_TRACING=true@LANGSMITH_PROJECT=aureon@ADMIN_USERS=$ADMIN_USERS@MARKET_DATA_PROVIDER=yfinance@FINR1_URL=$FINR1_URL@FINR1_AUTH=true@APP_ENV=prod$GEMINI_ENV" \
  --set-secrets "${GEMINI_SECRET}LANGSMITH_API_KEY=LANGSMITH_API_KEY:latest,DATABASE_URL=DATABASE_URL:latest,APP_PASSCODE=APP_PASSCODE:latest,ADMIN_PASSCODE=ADMIN_PASSCODE:latest,SECRET_KEY=SECRET_KEY:latest,TICK_TOKEN=TICK_TOKEN:latest"
URL=$(gcloud run services describe "$APP" --region "$REGION" --format 'value(status.url)')

echo "==> Cloud Scheduler: market scan every 30 min, 09:30-15:00 IST, Mon-Fri"
TOKEN=$(gcloud secrets versions access latest --secret TICK_TOKEN)
gcloud scheduler jobs delete aureon-scan --location "$REGION" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http aureon-scan --location "$REGION" \
  --schedule "*/30 9-14 * * 1-5" --time-zone "Asia/Kolkata" --uri "$URL/api/autonomous/tick" \
  --http-method POST --headers "X-Tick-Token=$TOKEN" --attempt-deadline 1800s >/dev/null
echo "==> Cloud Scheduler: end-of-day Performance Analyst review, 15:45 IST Mon-Fri"
gcloud scheduler jobs delete aureon-review --location "$REGION" --quiet >/dev/null 2>&1 || true
gcloud scheduler jobs create http aureon-review --location "$REGION" \
  --schedule "45 15 * * 1-5" --time-zone "Asia/Kolkata" --uri "$URL/api/autonomous/tick?mode=review" \
  --http-method POST --headers "X-Tick-Token=$TOKEN" --attempt-deadline 1800s >/dev/null

echo "==> Monitoring: uptime check on /healthz"
gcloud monitoring uptime create aureon-uptime --resource-type uptime-url \
  --resource-labels "host=${URL#https://},project_id=$PROJECT_ID" --path /healthz --protocol https \
  --period 5 >/dev/null 2>&1 || echo "   (uptime check exists or needs console setup)"

if [[ -n "${BILLING_ACCOUNT:-}" ]]; then
  echo "==> Budget alerts at 50/80/100% of \$30"
  gcloud billing budgets create --billing-account "$BILLING_ACCOUNT" --display-name "aureon-30usd" \
    --budget-amount 30USD --threshold-rule percent=0.5 --threshold-rule percent=0.8 --threshold-rule percent=1.0 \
    >/dev/null 2>&1 || echo "   (budget exists or needs Billing permissions)"
fi

echo "==> Live diagnostics: database, market data, news, FinBERT, Whisper, MCP, every Gemini model (1-2 min)"
curl -s -m 600 -X POST -H "X-Tick-Token: $TOKEN" "$URL/api/autonomous/tick?mode=diag" -o /tmp/aureon-diag.json \
  && python3 -c "
import json; d = json.load(open('/tmp/aureon-diag.json'))
for r in d.get('results', []):
    print(('   PASS ' if r['ok'] else '   FAIL ') + r['check'].ljust(30) + str(r['seconds']).rjust(6) + 's  ' + ('' if r['ok'] else r['detail']))
print('   ' + ('ALL CHECKS PASSED' if d.get('all_ok') else 'CORE OK (failures above have fallbacks)' if d.get('critical_ok') else 'CORE FAILURE: paste the FAIL lines to get help'))
" || echo "   (diagnostics call failed: open Broker console → Run diagnostics)"
echo
echo
echo "✅ Aureon is live at: $URL"
echo "   Admin login: one of [$ADMIN_USERS] + your admin code. Clients: any username + client code."
echo "   Fin-R1:  ${FINR1_URL:-not deployed}"
echo "   Traces:  https://smith.langchain.com  (project: aureon)"
