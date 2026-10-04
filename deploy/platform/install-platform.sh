#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Provision the Debian platform VM: PostgreSQL 17 + pgvector, a virtualenv with
# the project installed, the tool registry schema, and the systemd units.
#
# No Docker. Run as root inside the platform VM:
#     sudo /opt/agentbox/install-platform.sh
# or from a checkout:
#     sudo deploy/platform/install-platform.sh
# ---------------------------------------------------------------------------
set -euo pipefail

DB_NAME="${DB_NAME:-agentbox}"
DB_USER="${DB_USER:-agent}"
DB_PASSWORD="${DB_PASSWORD:-agent}"
APP_USER="${APP_USER:-agent}"
# PyPI is as unreachable as deb.debian.org from some networks; default to a
# fast mirror and let the operator override it.
export PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
export PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
REPO_DIR="${REPO_DIR:-}"
SERVICE_DIR="/etc/systemd/system"

log() { printf '\033[36m[platform]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[platform] %s\033[0m\n' "$*" >&2; exit 1; }

[[ "${EUID}" -eq 0 ]] || die "must run as root"

if [[ -z "${REPO_DIR}" ]]; then
  for candidate in /opt/agentbox/app "${PWD}"; do
    if [[ -f "${candidate}/pyproject.toml" ]]; then REPO_DIR="${candidate}"; break; fi
  done
fi
[[ -n "${REPO_DIR}" && -f "${REPO_DIR}/pyproject.toml" ]] || die "cannot find the project; set REPO_DIR=/path/to/agentbox"

log "project directory: ${REPO_DIR}"

log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  postgresql postgresql-contrib postgresql-17-pgvector \
  python3 python3-venv python3-pip git curl ca-certificates jq
# build-essential is only needed if a dependency has no wheel for this platform;
# install-platform.sh retries with it below if pip fails to build anything.

log "starting PostgreSQL"
systemctl enable --now postgresql

log "creating role and database"
sudo -u postgres psql -v ON_ERROR_STOP=0 -tAc \
  "SELECT 1 FROM pg_roles WHERE rolname='${DB_USER}'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}'"
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}'" | grep -q 1 || \
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}"
  # a scratch database for the pg test suite (it wipes rows): never point it at the live one
  sudo -u postgres createdb -O "${DB_USER}" "${DB_NAME}_test" 2>/dev/null || true
  sudo -u postgres psql -d "${DB_NAME}_test" -c "create extension if not exists vector" >/dev/null 2>&1 || true
sudo -u postgres psql -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS vector" >/dev/null
sudo -u postgres psql -d "${DB_NAME}" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm" >/dev/null
log "extensions: $(sudo -u postgres psql -d "${DB_NAME}" -tAc "select string_agg(extname, ',') from pg_extension where extname in ('vector','pg_trgm')")"

pip_retry_with_compiler() {
  local target="$1"
  log "pip failed; installing build-essential/python3-dev and retrying once"
  apt-get install -y -qq --no-install-recommends build-essential python3-dev
  "${VENV}/bin/pip" install -e "${target}"
}

log "creating the virtualenv"
VENV="${REPO_DIR}/.venv"
python3 -m venv "${VENV}"
"${VENV}/bin/pip" install --quiet --upgrade pip
if [[ "${INSTALL_LOCAL_EMBED:-0}" == "1" ]]; then
  # CPU-only torch first: the default PyPI wheel bundles CUDA and is several GB
  # larger than anything this agent needs.
  # The aliyun CPU mirror has no cp313 wheels (measured: "Could not find a version that
# satisfies the requirement torch") and the default PyPI wheel bundles CUDA (several GB),
# so the official CPU index is the default.  Override with PYTORCH_INDEX=... if you like.
PYTORCH_INDEX="${PYTORCH_INDEX:-https://download.pytorch.org/whl/cpu}"
  log "installing CPU-only torch from ${PYTORCH_INDEX}"
  "${VENV}/bin/pip" install torch --index-url "${PYTORCH_INDEX}"
  log "installing sentence-transformers"
  "${VENV}/bin/pip" install -e "${REPO_DIR}[local-embed]" || pip_retry_with_compiler "${REPO_DIR}[local-embed]"
else
  "${VENV}/bin/pip" install -e "${REPO_DIR}" || pip_retry_with_compiler "${REPO_DIR}"
fi

log "writing .env (keep the API key secret)"
if [[ ! -f "${REPO_DIR}/.env" ]]; then
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
  python3 - "$REPO_DIR/.env" "$DB_USER" "$DB_PASSWORD" "$DB_NAME" <<'PY'
import secrets, sys
path, user, password, name = sys.argv[1:5]
text = open(path, encoding="utf-8").read()
text = text.replace("postgresql+asyncpg://agent:agent@127.0.0.1:5432/agentbox?ssl=disable",
                    f"postgresql+asyncpg://{user}:{password}@127.0.0.1:5432/{name}?ssl=disable")
text = text.replace("AGENT_CONTROL_SECRET=", f"AGENT_CONTROL_SECRET={secrets.token_hex(32)}")
open(path, "w", encoding="utf-8").write(text)
print(f"generated control secret in {path}")
PY
  chmod 640 "${REPO_DIR}/.env"
  if id "${APP_USER}" >/dev/null 2>&1; then
    chown "${APP_USER}:${APP_USER}" "${REPO_DIR}/.env"
  fi
else
  log ".env already exists, leaving it alone"
fi

# The bge-m3 weights (~2.3 GB) are fetched on first use.  Point huggingface_hub
# at a reachable mirror (systemd passes .env into the service environment).
if [[ "${INSTALL_LOCAL_EMBED:-0}" == "1" ]]; then
  if ! grep -q '^HF_ENDPOINT=' "${REPO_DIR}/.env" 2>/dev/null; then
    printf 'HF_ENDPOINT=%s\n' "${HF_ENDPOINT:-https://hf-mirror.com}" >> "${REPO_DIR}/.env"
    log "set HF_ENDPOINT in .env (first search downloads the model through it)"
  fi
fi

mkdir -p "${REPO_DIR}/var"
if id "${APP_USER}" >/dev/null 2>&1; then
  chown -R "${APP_USER}:${APP_USER}" "${REPO_DIR}/var"
fi

log "initialising the registry schema and seeding core tools"
cd "${REPO_DIR}"
"${VENV}/bin/python" -m agent.cli db init
"${VENV}/bin/python" -m agent.cli db seed

log "installing systemd units"
install -m 0644 "${REPO_DIR}/deploy/systemd/agentbox-ai.service" "${SERVICE_DIR}/agentbox-ai.service"
systemctl daemon-reload
systemctl enable agentbox-ai.service

# The control plane belongs on the *host*; on Linux it is a second unit so the
# whole stack also runs on a bare Debian machine.
install -m 0644 "${REPO_DIR}/deploy/systemd/agentbox-control.service" "${SERVICE_DIR}/agentbox-control.service"
systemctl daemon-reload

cat <<EOF

platform provisioning done.

  edit       ${REPO_DIR}/.env   (AGENT_LLM_API_KEY, AGENT_CONTROL_URL, AGENT_CONTROL_SECRET)
  build      sudo ${REPO_DIR}/deploy/sandbox/build-sandbox-image.sh
  start      sudo systemctl start agentbox-ai
  logs       journalctl -u agentbox-ai -f
  verify     ${VENV}/bin/python -m agent.cli doctor

The control plane runs on the Windows host:
  pwsh> python -m agent.cli serve control
and the AI service reaches it at AGENT_CONTROL_URL (default http://10.0.2.2:8091).
Set the same AGENT_CONTROL_SECRET on both sides.
EOF
