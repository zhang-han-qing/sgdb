#!/usr/bin/env bash
set -eu

# Install multitui runtime and sgdb wrapper for debugger host.
# Default is user-local install (no sudo required).
#
# Optional env overrides:
#   INSTALL_ROOT=/path/to/install
#   BIN_DIR=/path/to/bin
#   VENV_DIR=/path/to/venv
#
# Usage:
#   ./setup_gdb_host.sh

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
INSTALL_ROOT="${INSTALL_ROOT:-${HOME}/.local/share/sgdb}"
if [ -z "${BIN_DIR:-}" ]; then
    # Product default: put command in a standard PATH location when possible.
    if [ -d "/usr/local/bin" ] && [ -w "/usr/local/bin" ]; then
        BIN_DIR="/usr/local/bin"
    else
        BIN_DIR="${HOME}/.local/bin"
    fi
fi
VENV_DIR="${VENV_DIR:-${INSTALL_ROOT}/.venv-multitui}"

echo "[setup_gdb_host] install root: ${INSTALL_ROOT}"
mkdir -p "${INSTALL_ROOT}" "${BIN_DIR}"

# Sync required runtime folders.
rm -rf "${INSTALL_ROOT}/multitui" "${INSTALL_ROOT}/plugins"
cp -a "${SCRIPT_DIR}/multitui" "${INSTALL_ROOT}/"
cp -a "${SCRIPT_DIR}/plugins" "${INSTALL_ROOT}/"

echo "[setup_gdb_host] create/update venv: ${VENV_DIR}"
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip >/dev/null
"${VENV_DIR}/bin/pip" install pyte textual >/dev/null

echo "[setup_gdb_host] install sgdb gdb plugins (~/.gdbinit)"
SGDB_GDBINIT_PATH="${INSTALL_ROOT}/plugins/gdbinit.py" \
    bash "${INSTALL_ROOT}/plugins/install.sh"

WRAPPER="${BIN_DIR}/sgdb"
cat > "${WRAPPER}" <<EOF
#!/usr/bin/env bash
set -eu

SGDB_ROOT="${INSTALL_ROOT}"
SGDB_VENV="${VENV_DIR}"
export PYTHONPATH="\${SGDB_ROOT}\${PYTHONPATH:+:\${PYTHONPATH}}"

sub="\${1:-}"
case "\${sub}" in
    multiui)
        shift
        exec "\${SGDB_VENV}/bin/python" -m multitui "\$@"
        ;;
    ""|-h|--help|help)
        cat <<'HELP'
Usage:
  sgdb multiui [multitui args...]

Examples:
  sgdb multiui --ip 172.24.12.100 --device 1690 --cores 8 --gdb gdb-multiarch
HELP
        ;;
    *)
        echo "sgdb: unknown subcommand: \${sub}" >&2
        echo "try: sgdb --help" >&2
        exit 2
        ;;
esac
EOF
chmod +x "${WRAPPER}"

echo
echo "[setup_gdb_host] done."
echo "Wrapper: ${WRAPPER}"
if command -v sgdb >/dev/null 2>&1; then
    echo "READY: sgdb is available now."
    echo "Try:"
    echo "  sgdb --help"
else
    echo "READY: you can run sgdb via absolute path:"
    echo "  ${WRAPPER} --help"
    echo "If you want plain 'sgdb', add PATH (once):"
    echo "  export PATH=\"${BIN_DIR}:\$PATH\""
fi
