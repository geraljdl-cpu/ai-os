#!/usr/bin/env bash
set -euo pipefail

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
AIOS_AUTOFILL_N="${AIOS_AUTOFILL_N:-42}"

PYTHONPATH="$AIOS_ROOT" python3 - <<'PY'
import os
from bin import backlog_pg

n = int(os.environ.get("AIOS_AUTOFILL_N","42"))

# tenta funções conhecidas; se não existirem, não rebenta
fn = None
for name in ("autofill_if_empty", "autofill", "auto_refill_if_empty"):
    if hasattr(backlog_pg, name):
        fn = getattr(backlog_pg, name)
        break

if fn is None:
    print("AUTOFILL_NOFUNC")
    raise SystemExit(0)

# chama com assinatura flexível
try:
    fn(n)                 # autofill_if_empty(n)
except TypeError:
    try:
        fn(target=n)      # autofill_if_empty(target=n)
    except TypeError:
        fn()              # autofill_if_empty()
print("AUTOFILL_OK")
PY
