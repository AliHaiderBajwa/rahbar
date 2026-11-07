#!/usr/bin/env bash
# Our laptop cluster: k3d instead of minikube (3.8 GB of RAM, no minikube installed).
# k3s enforces NetworkPolicy out of the box, like minikube with --cni=calico.
#
# - a Docker network with a fixed subnet, shared by the k3d node and Ollama
# - Ollama (embeddings only) as a container at 172.30.0.200 on that network, so it never
#   listens on the laptop's own interfaces; overlays/k3d points OLLAMA_URL at it
# - the Ingress on 127.0.0.1:8080 only
set -euo pipefail
NET=rahbar-net
OLLAMA_DIR=${OLLAMA_DIR:-$HOME/.local/opt/ollama}     # an unpacked Ollama release
MODELS_DIR=${MODELS_DIR:-$HOME/.ollama/models}

docker network inspect $NET >/dev/null 2>&1 || \
  docker network create --subnet 172.30.0.0/16 --gateway 172.30.0.1 $NET

if ! docker ps --format '{{.Names}}' | grep -qx rahbar-ollama; then
  docker rm -f rahbar-ollama >/dev/null 2>&1 || true
  docker run -d --name rahbar-ollama --network $NET --ip 172.30.0.200 --restart unless-stopped \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -e OLLAMA_HOST=0.0.0.0:11434 \
    -e OLLAMA_MODELS=/models -e OLLAMA_KEEP_ALIVE=24h \
    -v "$OLLAMA_DIR":/opt/ollama:ro -v "$MODELS_DIR":/models \
    python:3.12.16-slim-trixie /opt/ollama/bin/ollama serve
fi

k3d cluster list rahbar >/dev/null 2>&1 || \
  k3d cluster create rahbar --network $NET --servers 1 --agents 0 \
    --api-port 127.0.0.1:6550 -p "127.0.0.1:8080:80@loadbalancer" \
    --k3s-arg "--disable=traefik@server:0" --wait

kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.12.1/deploy/static/provider/cloud/deploy.yaml
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s
docker exec rahbar-ollama /opt/ollama/bin/ollama pull nomic-embed-text
echo "cluster up: Ingress on http://127.0.0.1:8080 (Host: rahbar.local)"
