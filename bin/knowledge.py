#!/usr/bin/env python3
"""
knowledge.py — Simple knowledge layer using Qdrant + nomic-embed-text (ollama)

Commands:
  add <kind> <text>          — store a knowledge entry
  search <query>             — semantic search
  list [kind]                — list entries
  delete <id>                — delete entry by UUID

Kinds: decision | note | document | idea | task | briefing

Usage:
  python3 bin/knowledge.py add decision "Decidimos usar Qdrant como memória do sistema"
  python3 bin/knowledge.py search "decisões sobre infraestrutura"
  python3 bin/knowledge.py list decision
"""
import sys, os, json, uuid, time, argparse, urllib.request, urllib.parse

QDRANT_URL  = os.environ.get("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://localhost:11434")
COLLECTION  = "knowledge"
EMBED_MODEL = "nomic-embed-text"
VECTOR_SIZE = 768


def _http(method: str, url: str, body=None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req  = urllib.request.Request(url, data=data, method=method,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _embed(text: str) -> list[float]:
    resp = _http("POST", f"{OLLAMA_URL}/api/embeddings",
                 {"model": EMBED_MODEL, "prompt": text})
    return resp["embedding"]


def _ensure_collection():
    try:
        _http("GET", f"{QDRANT_URL}/collections/{COLLECTION}")
    except Exception:
        _http("PUT", f"{QDRANT_URL}/collections/{COLLECTION}", {
            "vectors": {"size": VECTOR_SIZE, "distance": "Cosine"},
            "on_disk_payload": True,
        })


def cmd_add(kind: str, text: str, meta: dict | None = None) -> str:
    _ensure_collection()
    vec = _embed(text)
    point_id = str(uuid.uuid4())
    payload  = {
        "kind":    kind,
        "text":    text,
        "ts":      time.time(),
        "meta":    meta or {},
    }
    _http("PUT", f"{QDRANT_URL}/collections/{COLLECTION}/points", {
        "points": [{"id": point_id, "vector": vec, "payload": payload}]
    })
    return point_id


def cmd_search(query: str, limit: int = 5, kind: str | None = None) -> list[dict]:
    _ensure_collection()
    vec    = _embed(query)
    body   = {"vector": vec, "limit": limit, "with_payload": True, "with_vector": False}
    if kind:
        body["filter"] = {"must": [{"key": "kind", "match": {"value": kind}}]}
    resp   = _http("POST", f"{QDRANT_URL}/collections/{COLLECTION}/points/search", body)
    return [
        {
            "id":    p["id"],
            "score": round(p["score"], 3),
            "kind":  p["payload"]["kind"],
            "text":  p["payload"]["text"],
            "ts":    p["payload"].get("ts"),
        }
        for p in resp.get("result", [])
    ]


def cmd_list(kind: str | None = None, limit: int = 20) -> list[dict]:
    _ensure_collection()
    body = {"limit": limit, "with_payload": True, "with_vector": False}
    if kind:
        body["filter"] = {"must": [{"key": "kind", "match": {"value": kind}}]}
    resp = _http("POST", f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll", body)
    return [
        {
            "id":   p["id"],
            "kind": p["payload"]["kind"],
            "text": p["payload"]["text"][:120],
            "ts":   p["payload"].get("ts"),
        }
        for p in resp.get("result", {}).get("points", [])
    ]


def cmd_delete(point_id: str) -> bool:
    _http("POST", f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
          {"points": [point_id]})
    return True


def cmd_stats() -> dict:
    try:
        resp = _http("GET", f"{QDRANT_URL}/collections/{COLLECTION}")
        r    = resp.get("result", {})
        return {
            "collection": COLLECTION,
            "points":     r.get("points_count", 0),
            "status":     r.get("status", "unknown"),
        }
    except Exception as e:
        return {"collection": COLLECTION, "error": str(e)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    sub    = parser.add_subparsers(dest="cmd")

    p_add = sub.add_parser("add")
    p_add.add_argument("kind", choices=["decision", "note", "document", "idea", "task", "briefing"])
    p_add.add_argument("text")
    p_add.add_argument("--meta", default="{}")

    p_search = sub.add_parser("search")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=5)
    p_search.add_argument("--kind", default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("kind", nargs="?", default=None)
    p_list.add_argument("--limit", type=int, default=20)

    p_del = sub.add_parser("delete")
    p_del.add_argument("id")

    p_stats = sub.add_parser("stats")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    if args.cmd == "add":
        meta = json.loads(args.meta)
        pid  = cmd_add(args.kind, args.text, meta)
        print(json.dumps({"ok": True, "id": pid}))

    elif args.cmd == "search":
        results = cmd_search(args.query, args.limit, args.kind)
        print(json.dumps(results, ensure_ascii=False, indent=2))

    elif args.cmd == "list":
        items = cmd_list(args.kind, args.limit)
        print(json.dumps(items, ensure_ascii=False, indent=2))

    elif args.cmd == "delete":
        cmd_delete(args.id)
        print(json.dumps({"ok": True}))

    elif args.cmd == "stats":
        print(json.dumps(cmd_stats()))
