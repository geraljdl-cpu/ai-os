#!/usr/bin/env python3
import json, time, urllib.request, argparse, os

def post_json(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"raw": raw, "status": getattr(resp, "status", None)}

def load_tasks(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("AIOS_BASE", "http://localhost:5679"))
    ap.add_argument("--tasks", default=os.environ.get("AIOS_TASKS", "/home/jdl/ai-os/runtime/tasks.json"))
    ap.add_argument("--state", default=os.environ.get("AIOS_STATE", "/home/jdl/ai-os/runtime/state.json"))
    ap.add_argument("--tick", type=int, default=5)
    args = ap.parse_args()

    state = {"last_run": {}, "last_result": {}}
    if os.path.exists(args.state):
        try:
            state = json.load(open(args.state, "r", encoding="utf-8"))
        except Exception:
            state = {"last_run": {}, "last_result": {}}

    print("autopilot-daemon base=", args.base, "tasks=", args.tasks)

    while True:
        try:
            spec = load_tasks(args.tasks)
            now = int(time.time())
            changed = False

            for t in spec.get("tasks", []):
                if not t.get("enabled", True):
                    continue
                if t.get("type") != "autopilot":
                    continue

                tid = t.get("id", "task")
                every = int(t.get("every_seconds", 300))
                last = int(state.get("last_run", {}).get(tid, 0))
                if now - last < every:
                    continue

                goal = t.get("goal", "Faz smoke e diz se está OK.")
                out = post_json(args.base + "/smoke", {}, timeout=60)
                state.setdefault("last_run", {})[tid] = now
                state.setdefault("last_result", {})[tid] = out
                changed = True

                ok = out.get("ok")
                msg = out.get("answer") or out.get("error") or ""
                if not msg and isinstance(out.get("results"), list):
                    msg = "checks=" + str(len(out.get("results", [])))
                msg = msg.replace("\n", " ")
                print(time.strftime("%F %T"), tid, "ok=" + str(ok), (msg[:160] + ("..." if len(msg) > 160 else "")))

            if changed:
                save_state(args.state, state)

        except Exception as e:
            print(time.strftime("%F %T"), "daemon_error:", repr(e))

        time.sleep(args.tick)

if __name__ == "__main__":
    main()
