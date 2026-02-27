#!/usr/bin/env python3
import argparse, json, os, subprocess, sys, textwrap, time
from urllib.request import Request, urlopen

DEFAULT_URL = os.environ.get("AIOS_ROUTER_URL", "http://localhost:5679")

def sh(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed: {cmd}\n---stdout---\n{p.stdout}\n---stderr---\n{p.stderr}")
    return p.stdout.strip()

def http_post_json(url, payload, timeout=600):
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type":"application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def cmd_dev(args):
    payload = {
        "repo_path": os.path.abspath(args.repo),
        "request": args.request,
        "base_branch": args.base_branch,
        "mode": args.mode,
        "ai": True,
    }
    out = http_post_json(f"{args.url}/jobs/dev", payload, timeout=args.timeout)
    print(json.dumps(out, indent=2, ensure_ascii=False))

def cmd_jobs(args):
    out = http_post_json(f"{args.url}/jobs/list", {}, timeout=args.timeout)
    print(json.dumps(out, indent=2, ensure_ascii=False))

def main():
    ap = argparse.ArgumentParser(prog="aios", formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"agent-router url (default: {DEFAULT_URL})")
    ap.add_argument("--timeout", type=int, default=1200, help="http timeout seconds")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pdev = sub.add_parser("dev", help="run a dev job (no copy/paste)")
    pdev.add_argument("request", help="what to build/fix")
    pdev.add_argument("--repo", default=".", help="repo path (default: .)")
    pdev.add_argument("--base-branch", default="main", help="base branch (default: main)")
    pdev.add_argument("--mode", default="openai", help="agent mode (default: openai)")
    pdev.set_defaults(func=cmd_dev)

    pjobs = sub.add_parser("jobs", help="list recent jobs")
    pjobs.set_defaults(func=cmd_jobs)

    args = ap.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()
