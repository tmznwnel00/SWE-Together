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
