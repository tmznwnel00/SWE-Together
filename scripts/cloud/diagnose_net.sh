#!/usr/bin/env bash
# Where does a task container's network break? Run in the cloud session (Docker daemon up, one task image present).
# Prints status codes only; proxy credentials in URLs are masked.
set -u
URLS="https://claude.ai/install.sh https://downloads.claude.ai/claude-code-releases/latest https://api.anthropic.com https://openrouter.ai https://ghcr.io/v2/ https://registry.npmjs.org https://pypi.org"
mask() { sed -E 's#(//)[^@/ ]*@#\1***@#g'; }
probe() {   # probe <label> <docker run extra args...>   (empty args = run on the host)
  label=$1; shift
  echo "== $label"
  for u in $URLS; do
    if [ $# -eq 0 ]; then out=$(curl -sS -o /dev/null -m 15 -w '%{http_code}' "$u" 2>&1 | tail -1)
    else out=$(docker run --rm "$@" "$IMG" sh -c "curl -sS -o /dev/null -m 15 -w '%{http_code}' '$u' 2>&1 | tail -1" 2>&1 | tail -1); fi
    printf '  %-55s %s\n' "$u" "$out"
  done
}
echo "== proxy-related env on the host"
env | grep -iE '^(https?_proxy|all_proxy|no_proxy|CLAUDE_CODE_PROXY|SSL_CERT|NODE_EXTRA)' | mask
IMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep multi-user-turn-codebench | head -1)
[ -n "$IMG" ] || { echo "no task image present: pull one first (docker pull ghcr.io/togetherbench/multi-user-turn-codebench/agent-swarm-task-4a881b)"; exit 1; }
echo "image: $IMG"
CA=$HOME/.metaharness/bench/ca-bundle.pem
CAARGS=""; [ -f "$CA" ] && CAARGS="-v $CA:/etc/ssl/certs/metaharness-ca-bundle.pem:ro -e SSL_CERT_FILE=/etc/ssl/certs/metaharness-ca-bundle.pem -e CURL_CA_BUNDLE=/etc/ssl/certs/metaharness-ca-bundle.pem"
probe "host"
probe "container, default bridge network (what the benchmark does now)" $CAARGS
probe "container, --network host" --network host $CAARGS
if [ -n "${HTTPS_PROXY:-${https_proxy:-}}" ]; then
  P=${HTTPS_PROXY:-$https_proxy}
  probe "container, bridge + proxy env passed in" $CAARGS -e HTTPS_PROXY="$P" -e HTTP_PROXY="$P" -e https_proxy="$P" -e http_proxy="$P"
  probe "container, --network host + proxy env passed in" --network host $CAARGS -e HTTPS_PROXY="$P" -e HTTP_PROXY="$P" -e https_proxy="$P" -e http_proxy="$P"
fi
