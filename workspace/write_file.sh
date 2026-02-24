#!/usr/bin/env bash
set -euo pipefail
path="$1"
content="$2"
python3 - <<PY
import json, urllib.request
path = ${path@Q}
content = ${content@Q}
data = json.dumps({"path": path.strip("'"), "content": content.strip("'")}).encode("utf-8")
req = urllib.request.Request("http://127.0.0.1:8020/write", data=data, headers={"Content-Type":"application/json"})
print(urllib.request.urlopen(req).read().decode())
PY
