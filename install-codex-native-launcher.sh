#!/bin/sh
set -eu

package_root=$1
launcher=$2
runtime_root=${3:-$package_root}
set -- "$package_root"/node_modules/@openai/codex-linux-*/vendor/*/bin/codex

if [ "$#" -ne 1 ] || [ ! -x "$1" ] || [ ! -x "${1%/*}/codex-code-mode-host" ]; then
    echo "unsupported Codex package layout: expected one native binary and companion" >&2
    exit 1
fi

native=$1
case "$package_root$native$runtime_root" in
    *"'"*) echo "unsupported Codex package layout: quote in path" >&2; exit 1 ;;
esac
runtime_native=$runtime_root${native#"$package_root"}

rm -f "$launcher"
umask 022
printf '%s\n' \
    '#!/bin/sh' \
    'unset CODEX_MANAGED_BY_NPM CODEX_MANAGED_BY_BUN CODEX_MANAGED_BY_PNPM CODEX_MANAGED_BY_VITE_PLUS' \
    'export CODEX_MANAGED_BY_NPM=1' \
    "export CODEX_MANAGED_PACKAGE_ROOT='$runtime_root'" \
    "exec '$runtime_native' \"\$@\"" > "$launcher"
chmod 0755 "$launcher"
