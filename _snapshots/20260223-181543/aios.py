#!/usr/bin/env python3
import argparse
import json
import sys
import urllib.request
import urllib.parse

DEFAULT_BASE = "http://localhost:5679"

def req(base: str, method: str, path: str, payload=None):
    url = base + path
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        raw = resp.read()
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            return json.loads(raw.decode("utf-8"))
        return raw.decode("utf-8", errors="replace")

def pretty(x):
    print(json.dumps(x, indent=2, ensure_ascii=False))

def main():
    ap = argparse.ArgumentParser(prog="aios", description="AI-OS control CLI")
    ap.add_argument("--base", default=DEFAULT_BASE)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    sub.add_parser("status")

    p_logs = sub.add_parser("logs")
    p_logs.add_argument("service")
    p_logs.add_argument("--tail", type=int, default=200)

    sub.add_parser("smoke")

    p_bash = sub.add_parser("bash")
    p_bash.add_argument("command", nargs=argparse.REMAINDER)

    p_agent = sub.add_parser("agent")
    p_agent.add_argument("text", nargs=argparse.REMAINDER)

    args = ap.parse_args()
    base = args.base

    if args.cmd == "health":
        pretty(req(base, "GET", "/health"))
        return

    if args.cmd == "status":
        pretty(req(base, "GET", "/status"))
        return

    if args.cmd == "logs":
        path = f"/logs/{urllib.parse.quote(args.service)}?tail={args.tail}"
        pretty(req(base, "GET", path))
        return
    if args.cmd == "smoke":
        pretty(req(base, "POST", "/smoke", {}))
        return

    if args.cmd == "bash":
        if not args.command:
            print("usage: aios bash ls -la")
            sys.exit(2)
        cmd = " ".join(args.command)
        pretty(req(base, "POST", "/bash", {"cmd": cmd}))
        return

    if args.cmd == "agent":
        if not args.text:
            print("usage: aios agent <texto>")
            sys.exit(2)
        txt = " ".join(args.text)
        pretty(req(base, "POST", "/agent", {"chatInput": txt, "mode": "openai"}))
        return

if __name__ == "__main__":
    main()
