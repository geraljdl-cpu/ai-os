#!/usr/bin/env python3
"""
toc_oauth.py — Toconline OAuth 2.0 token manager

Fluxo:
  1. python3 bin/toc_oauth.py login   → abre URL + servidor local port 8081 para callback
  2. Utilizador autoriza no browser
  3. Token guardado em .toc_token.json (access_token + refresh_token)
  4. python3 bin/toc_oauth.py refresh → renova token com refresh_token
  5. python3 bin/toc_oauth.py status  → estado actual do token
"""
import http.server, json, os, sys, time, urllib.parse, urllib.request, webbrowser

# ── Config ────────────────────────────────────────────────────────────────────

_env = "/etc/aios.env"
if os.path.exists(_env):
    for _l in open(_env):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            k, _, v = _l.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

CLIENT_ID     = os.environ.get("TOC_CLIENT_ID",     "pt515472514_c157987-a3981ff4471b01f9")
CLIENT_SECRET = os.environ.get("TOC_CLIENT_SECRET",  "d92296d88bcf055350e6ca19054879be")
OAUTH_URL     = os.environ.get("TOC_OAUTH_URL",      "https://app29.toconline.pt/oauth")
REDIRECT_URI  = os.environ.get("TOC_REDIRECT_URI",   "http://localhost:8081/callback")
TOKEN_FILE    = os.path.expanduser("~/ai-os/.toc_token.json")


def save_token(t: dict):
    json.dump(t, open(TOKEN_FILE, "w"), indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    exp = t.get("expires_in", 14400)
    print(f"[toc_oauth] Token guardado — expira em {exp//60} min")


def load_token() -> dict:
    try:
        return json.load(open(TOKEN_FILE))
    except Exception:
        return {}


def token_expiry() -> int:
    """Returns UNIX timestamp of token expiry (from file mtime + expires_in)."""
    t = load_token()
    if not t:
        return 0
    mtime = os.path.getmtime(TOKEN_FILE)
    return int(mtime) + int(t.get("expires_in", 14400)) - 300


def is_valid() -> bool:
    return time.time() < token_expiry()


def refresh() -> bool:
    t = load_token()
    rt = t.get("refresh_token")
    if not rt:
        print("[toc_oauth] Sem refresh_token — necessário novo login")
        return False
    data = urllib.parse.urlencode({
        "grant_type":    "refresh_token",
        "refresh_token": rt,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    try:
        req = urllib.request.Request(
            f"{OAUTH_URL}/token", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        resp = urllib.request.urlopen(req, timeout=15)
        new_token = json.loads(resp.read())
        save_token(new_token)
        return True
    except Exception as e:
        print(f"[toc_oauth] Refresh falhou: {e}")
        return False


def ensure_valid() -> bool:
    if is_valid():
        return True
    print("[toc_oauth] Token expirado, a tentar refresh...")
    return refresh()


def get_access_token() -> str | None:
    ensure_valid()
    t = load_token()
    return t.get("access_token")


def login():
    """Start OAuth2 authorization code flow — opens browser + local callback server."""
    auth_url = (
        f"{OAUTH_URL}/authorize"
        f"?response_type=code"
        f"&client_id={urllib.parse.quote(CLIENT_ID)}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&scope=openid"
    )
    print(f"[toc_oauth] Abre este URL no browser:\n{auth_url}\n")

    code_holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = dict(urllib.parse.parse_qsl(parsed.query))
            if "code" in params:
                code_holder["code"] = params["code"]
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h2>Toconline autorizado! Podes fechar esta janela.</h2>")
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Erro: code em falta")

        def log_message(self, *_):
            pass

    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    print("[toc_oauth] A aguardar callback em http://localhost:8081/callback ...")
    server = http.server.HTTPServer(("localhost", 8081), Handler)
    server.timeout = 120
    while "code" not in code_holder:
        server.handle_request()
    server.server_close()

    code = code_holder["code"]
    print(f"[toc_oauth] Código recebido, a trocar por token...")

    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        f"{OAUTH_URL}/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    token = json.loads(resp.read())
    save_token(token)
    print(f"[toc_oauth] ✅ Login Toconline OK — access_token obtido")
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "login":
        login()
    elif cmd == "refresh":
        ok = refresh()
        sys.exit(0 if ok else 1)
    elif cmd == "status":
        t = load_token()
        if not t:
            print("sem token")
            sys.exit(1)
        valid = is_valid()
        exp   = token_expiry()
        mins  = max(0, (exp - int(time.time())) // 60)
        print(json.dumps({
            "valid": valid,
            "access_token": t.get("access_token","")[:20] + "...",
            "has_refresh": bool(t.get("refresh_token")),
            "expires_in_min": mins if valid else 0,
        }))
        sys.exit(0 if valid else 1)
    else:
        print(f"Usage: toc_oauth.py login|refresh|status")
        sys.exit(1)
