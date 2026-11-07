#!/usr/bin/env bash
# Ephemeral-cluster end-to-end run, shared by ci.yml (e2e-eval) and cd.yml (deploy).
#
#   scripts/ci/k3d-e2e.sh <api-image> <ui-image> <indexer-image> [--import]
#
# 1. Ollama on the runner (embeddings only), listening for the cluster
# 2. a k3d cluster (k3s enforces NetworkPolicy) + ingress-nginx, Ingress on :8080
# 3. the ci overlay: fixture corpus, stub LLM, images pinned to the given tags
# 4. waits for the indexer Job and the api rollout, then answers through the Ingress
# --import loads locally built images into k3d (CI); without it k3d pulls them (CD).
set -euo pipefail

API_IMAGE=$1; UI_IMAGE=$2; INDEXER_IMAGE=$3; MODE=${4:-}
NS=rahbar
K3D_VERSION=v5.8.3
INGRESS_NGINX=controller-v1.12.1
EMBED_MODEL=nomic-embed-text

log() { printf '\n== %s\n' "$*"; }

log "Ollama for embeddings"
if ! command -v ollama >/dev/null; then
  curl -fsSL https://ollama.com/install.sh | sh >/dev/null
fi
sudo systemctl stop ollama 2>/dev/null || true
# The runner is a throwaway VM; the k3d nodes reach it through the Docker bridge.
OLLAMA_HOST=0.0.0.0:11434 nohup ollama serve >/tmp/ollama.log 2>&1 &
for _ in $(seq 1 30); do curl -sf localhost:11434/api/version >/dev/null && break; sleep 1; done
ollama pull "$EMBED_MODEL" >/dev/null

log "k3d cluster"
if ! command -v k3d >/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | TAG=$K3D_VERSION bash
fi
k3d cluster create rahbar --wait --timeout 180s \
  --k3s-arg "--disable=traefik@server:0" -p "8080:80@loadbalancer"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX}/deploy/static/provider/cloud/deploy.yaml"
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s
kubectl -n ingress-nginx wait --for=condition=complete job --all --timeout=120s || true

if [ "$MODE" = "--import" ]; then
  log "importing locally built images"
  k3d image import -c rahbar "$API_IMAGE" "$UI_IMAGE" "$INDEXER_IMAGE"
fi

log "apply the ci overlay, images pinned by tag"
work=$(mktemp -d)
cp -r k8s "$work/"
( cd "$work/k8s/overlays/ci"
  kustomize edit set image "rahbar-api=$API_IMAGE" "rahbar-ui=$UI_IMAGE" "rahbar-indexer=$INDEXER_IMAGE" )
kustomize build "$work/k8s/overlays/ci" > /tmp/rahbar-ci.yaml
grep -E '^\s+image:' /tmp/rahbar-ci.yaml | sort -u
# The Ingress admission webhook can lag behind the controller rollout; retry once.
kubectl apply -f /tmp/rahbar-ci.yaml || { sleep 15; kubectl apply -f /tmp/rahbar-ci.yaml; }

log "indexer Job, then the api rollout"
if ! kubectl -n $NS wait --for=condition=complete job/indexer --timeout=420s; then
  kubectl -n $NS get pods -o wide
  kubectl -n $NS logs job/indexer --all-containers --tail=50 || true
  kubectl -n $NS logs statefulset/chroma --previous --tail=20 || true
  kubectl -n $NS get events --sort-by=.lastTimestamp | tail -25 || true
  exit 1
fi
kubectl -n $NS logs job/indexer | tail -3
kubectl -n $NS rollout status deploy/api --timeout=240s
kubectl -n $NS rollout status deploy/ui --timeout=120s
kubectl -n $NS get pods,svc,ingress,hpa

log "smoke test through the Ingress"
for _ in $(seq 1 30); do
  code=$(curl -s -o /tmp/ask.json -w '%{http_code}' -H 'Host: rahbar.local' \
    -H 'Content-Type: application/json' -X POST http://localhost:8080/api/ask \
    -d '{"question":"When is a student placed on academic warning?"}' || true)
  [ "$code" = "200" ] && break; sleep 4
done
python3 -m json.tool /tmp/ask.json
python3 - <<'PY'
import json
r = json.load(open("/tmp/ask.json"))
assert r["answered_from_context"] and r["citations"], "no grounded answer through the Ingress"
retrieved = {(x["source"], x["section"]) for x in r["retrieved"]}
assert all((c["source"], c["section"]) in retrieved for c in r["citations"]), "fabricated citation"
print("smoke test passed:", r["citations"][0])
PY
curl -sf -H 'Host: rahbar.local' http://localhost:8080/ | grep -q RAHBAR && echo "ui served at /"
