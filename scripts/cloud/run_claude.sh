#!/usr/bin/env bash
# Claude Code + claude-opus-5-5 on SWE-Together, in a cloud session, for a bounded time; results go to cloud_results/.
#
#   scripts/cloud/run_claude.sh [HOURS=1.9] [N_TASKS=30] [WORKERS=3]
#
# Needs two secrets in the cloud environment: SWT_OAUTH_TOKEN (Claude subscription token, from `claude setup-token`)
# and OPENROUTER_API_KEY (simulator + tagger, Gemini 3.1 Pro). Everything else is set here.
# Safe to run again: finished trials are skipped, unfinished ones are redone.
set -uo pipefail
HOURS=${1:-1.9}; N_TASKS=${2:-30}; WORKERS=${3:-3}
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
: "${SWT_OAUTH_TOKEN:?set SWT_OAUTH_TOKEN in the cloud environment}"
: "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY in the cloud environment}"

# 1. Docker daemon (off by default in the session)
if ! docker info >/dev/null 2>&1; then
  nohup dockerd >/tmp/dockerd.log 2>&1 &
  for _ in $(seq 40); do docker info >/dev/null 2>&1 && break; sleep 2; done
fi
docker info >/dev/null 2>&1 || { echo "docker daemon did not start (see /tmp/dockerd.log)"; exit 1; }

# 1b. The session reaches the internet through a proxy that (a) re-signs HTTPS with its own CA and (b) listens on 127.0.0.1 and
#     resolves host names itself. A task container on a bridge network trusts neither the CA nor can it reach that proxy, so the
#     in-container installs (claude.ai / downloads.claude.ai) fail before the agent starts. Fix = a Harbor compose overlay
#     (HARBOR_EXTRA_COMPOSE, picked up by swet_cli from ~/.metaharness/bench/swet-ca.yaml) that gives the containers the CA and the proxy.
#     CLOUD_NET=relay (default): containers stay on the bridge network and reach the proxy through scripts/cloud/proxy_relay.py.
#     CLOUD_NET=host: containers share the host network (verified by diagnose_net.sh, but parallel tasks share ports).
CA_SRC=${CA_BUNDLE:-/root/.ccr/ca-bundle.crt}
UP=${HTTPS_PROXY:-${https_proxy:-}}
CLOUD_NET=${CLOUD_NET:-relay}
if [ -f "$CA_SRC" ] || [ -n "$UP" ]; then
  mkdir -p "$HOME/.metaharness/bench"
  OV="$HOME/.metaharness/bench/swet-ca.yaml"
  { echo "services:"; echo "  main:"; } > "$OV"
  if [ -f "$CA_SRC" ]; then
    { [ -f /etc/ssl/certs/ca-certificates.crt ] && cat /etc/ssl/certs/ca-certificates.crt; cat "$CA_SRC"; } > "$HOME/.metaharness/bench/ca-bundle.pem"
    { echo "    volumes:"; echo "      - $HOME/.metaharness/bench/ca-bundle.pem:/etc/ssl/certs/metaharness-ca-bundle.pem:ro"; } >> "$OV"
  fi
  PROXY_IN_CONTAINER=""
  if [ -n "$UP" ]; then
    if [ "$CLOUD_NET" = host ]; then
      echo "    network_mode: host" >> "$OV"; PROXY_IN_CONTAINER="$UP"
    else
      RELAY_PORT=42224
      nohup python3 scripts/cloud/proxy_relay.py "$UP" "$RELAY_PORT" >/tmp/proxy_relay.log 2>&1 &
      echo '    extra_hosts: ["host.docker.internal:host-gateway"]' >> "$OV"
      PROXY_IN_CONTAINER=$(python3 scripts/cloud/proxy_relay.py --rewrite "$UP" host.docker.internal "$RELAY_PORT")
    fi
  fi
  echo "    environment:" >> "$OV"
  if [ -f "$CA_SRC" ]; then
    for v in SSL_CERT_FILE CURL_CA_BUNDLE NODE_EXTRA_CA_CERTS; do echo "      $v: /etc/ssl/certs/metaharness-ca-bundle.pem" >> "$OV"; done
  fi
  if [ -n "$PROXY_IN_CONTAINER" ]; then
    for v in HTTPS_PROXY HTTP_PROXY https_proxy http_proxy; do echo "      $v: \"$PROXY_IN_CONTAINER\"" >> "$OV"; done
    for v in NO_PROXY no_proxy; do echo "      $v: \"localhost,127.0.0.1\"" >> "$OV"; done
    echo '      CLAUDE_CODE_PROXY_RESOLVES_HOSTS: "true"' >> "$OV"
  fi
  echo "container network overlay written (CLOUD_NET=$CLOUD_NET, proxy=$([ -n "$UP" ] && echo yes || echo no), CA=$([ -f "$CA_SRC" ] && echo yes || echo no))"
else
  echo "no proxy and no CA bundle found: containers keep the default network"
fi

# 2. The benchmark. In the fork (this repo IS the benchmark with the local changes) nothing to do; next to the Meta-Harness
#    repo it is cloned at the commit the patches were made against, and the patches are applied.
if [ -f src/run_eval.py ]; then
  BENCH="$REPO"
else
  BENCH="$REPO/SWE-Together"
  if [ ! -d "$BENCH/.git" ]; then
    git clone https://github.com/Togetherbench/SWE-Together.git "$BENCH" || exit 1
    git -C "$BENCH" checkout -q ec7be3f50b004bccb58ab9f0769dece3fe82968c || exit 1
  fi
  if git -C "$BENCH" apply --check ../metaharness/bench/native_patches/0001-local-run.patch 2>/dev/null; then
    git -C "$BENCH" apply ../metaharness/bench/native_patches/0001-local-run.patch || exit 1
  fi
fi

# 3. Python environment of the benchmark
command -v uv >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh | sh; export PATH="$HOME/.local/bin:$PATH"; }
(cd "$BENCH" && uv sync) || exit 1
PY="$BENCH/.venv/bin/python"

# 4. Run settings (not secrets). No SWT_KEEP_IMAGES: the disk is small, Harbor deletes each image after its trial.
export SWT_SANDBOX=docker JUDGE_SANDBOX=docker SWT_LLM_BACKEND=native SWT_USER_SIM_BACKEND=openrouter
export SWT_TAGGER_BACKEND=openrouter SWT_JUDGE_BACKEND=native CLAUDE_CODE_EFFORT_LEVEL=high SWT_CC_VERSION=latest
export PYTHONPATH="$REPO"

# 5. The first N_TASKS tasks (canonical order) that have no finished trial yet
TASKS=$($PY - "$N_TASKS" "$BENCH" <<'EOF'
import json, sys
tasks = json.load(open(sys.argv[2] + "/canonical_full109.json"))["tasks"]
print(",".join(tasks[: int(sys.argv[1])]))
EOF
)
echo "running up to ${HOURS}h, ${WORKERS} workers, tasks: ${N_TASKS}"
timeout -s INT "$(python3 -c "print(int(float('$HOURS')*3600))")" \
  "$PY" -m metaharness.bench.native_run run --combo claude --tag nat_claude --tasks "$TASKS" --workers "$WORKERS"
echo "run ended (rc=$?)"

# 6. Package what finished and push it, so a local run can continue from here
"$PY" -m metaharness.bench.native_pack pack --tag nat_claude --out cloud_results/nat_claude
git add -f cloud_results/nat_claude && git commit -qm "cloud run: nat_claude trials" && git push || echo "push failed: copy cloud_results/nat_claude by hand"
