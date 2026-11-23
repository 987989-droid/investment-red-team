#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
HOOK_PATH="${REPO_ROOT}/.git/hooks/pre-commit"

cat > "${HOOK_PATH}" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

echo "[pre-commit] Running investment-red-team privacy-check on staged files..."
python3 -m cli.privacy_check --staged
EOF

chmod +x "${HOOK_PATH}"
echo "Installed pre-commit hook at ${HOOK_PATH}"
