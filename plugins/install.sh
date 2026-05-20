#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
SGDB_GDBINIT_PATH="${SGDB_GDBINIT_PATH:-${SCRIPT_DIR}/gdbinit.py}"
GDBINIT_FILE="${GDBINIT_FILE:-${HOME}/.gdbinit}"
LINE="source ${SGDB_GDBINIT_PATH}"

if [ ! -f "${SGDB_GDBINIT_PATH}" ]; then
    echo "[sgdb] error: gdbinit loader not found: ${SGDB_GDBINIT_PATH}" >&2
    exit 1
fi

if [ ! -f "${GDBINIT_FILE}" ]; then
    touch "${GDBINIT_FILE}"
fi

if grep -Fq "${LINE}" "${GDBINIT_FILE}"; then
    echo "[sgdb] already configured in ${GDBINIT_FILE}"
else
    printf "\n%s\n" "${LINE}" >> "${GDBINIT_FILE}"
    echo "[sgdb] added '${LINE}' into ${GDBINIT_FILE}"
fi

echo "[sgdb] done. restart gdb and run: help sgdb"
