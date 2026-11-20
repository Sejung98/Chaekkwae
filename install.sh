#!/usr/bin/env bash
# Chaekkwae install script (Linux / macOS; for Windows see README "Windows")
#   ./install.sh               # default install
#   PYTHON=python3.11 ./install.sh
#   ./install.sh --run-tests   # run unit tests after install
#   ./install.sh --probe       # call every database once and show which ones work from this machine
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYTHON="${PYTHON:-python3}"
RUN_TESTS=0
PROBE=0
for arg in "$@"; do
  case "$arg" in
    --run-tests) RUN_TESTS=1 ;;
    --probe) PROBE=1 ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '\033[1;34m[install]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ok]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 1. Python >= 3.10
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python not found: $PYTHON  (e.g. run module load python/3.11 and try again)" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Python 3.10 or later is required (current: $("$PYTHON" -V 2>&1))." >&2
  exit 1
fi
ok "Python: $("$PYTHON" -V 2>&1)"

# ---------------------------------------------------------------- 2. venv + dependencies
if [ ! -x .venv/bin/python ]; then
  say "Creating .venv"
  "$PYTHON" -m venv .venv
fi
say "Installing dependencies (mcp, httpx, pyyaml, pypdf, pytest, respx)"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e ".[test]"
ok "Package installed"

# ---------------------------------------------------------------- 3. config.yaml
if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  chmod 600 config.yaml
  ok "Created config.yaml (copied from config.example.yaml). Optional: set contact_email."
else
  ok "Keeping existing config.yaml"
fi

# ---------------------------------------------------------------- 4. MCP server loads
if N_TOOLS="$(.venv/bin/python -c 'import asyncio; from paperops_mcp import server; print(len(asyncio.run(server.server.list_tools())))' 2>&1)"; then
  ok "MCP server loads: $N_TOOLS tools (.mcp.json -> .venv/bin/python -m paperops_mcp.server)"
else
  warn "MCP server failed to load:"; printf '%s\n' "$N_TOOLS"
fi
command -v claude >/dev/null 2>&1 && ok "claude: $(command -v claude)" \
  || warn "claude not found -> install Claude Code: https://code.claude.com/docs (the CLI below works without it)"

# ---------------------------------------------------------------- 5. keys (names only; values are never read out)
say "API keys (environment variables). None is needed for PubMed, Europe PMC, PMC, bioRxiv, arXiv, Crossref."
for name in NCBI_API_KEY OPENALEX_API_KEY S2_API_KEY ELSEVIER_API_KEY ELSEVIER_INSTTOKEN WILEY_TDM_TOKEN SPRINGER_API_KEY; do
  if [ -n "${!name:-}" ]; then ok "$name is set"; else printf '       %s is not set\n' "$name"; fi
done

# ---------------------------------------------------------------- 6. optional checks
if [ "$RUN_TESTS" = 1 ]; then
  say "Running unit tests"
  .venv/bin/python -m pytest -q
fi
if [ "$PROBE" = 1 ]; then
  say "Probing every database (can take a minute or two when Europe PMC is slow)"
  .venv/bin/python -m paperops_mcp.cli check --probe --table
fi

cat <<EOF

Next steps
  1) (optional) put your contact e-mail in config.yaml, and export the API keys you have
  2) See what works from this machine:   .venv/bin/python -m paperops_mcp.cli check --probe --table
  3) Run claude in this folder:          cd "$ROOT" && claude
     On first launch, trust the folder and allow the paperops MCP server.
EOF
