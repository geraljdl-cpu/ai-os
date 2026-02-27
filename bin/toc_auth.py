#!/usr/bin/env python3
import json, os, time, urllib.request, urllib.parse, datetime

TOKEN_FILE  = os.path.expanduser("~/ai-os/.toc_token.json")
EXPIRY_FILE = os.path.expanduser("~/ai-os/.toc_token_expiry")
CLIENT_ID   = "pt515472514_c157987-a3981ff4471b01f9"
CLIENT_SECRET = "d92296d88bcf055350e6ca19054879be"
TOKEN_URL   = "https://app29.toconline.pt/oauth/token"

def save_token(t):
    json.dump(t, open(TOKEN_FILE,"w"), indent=2)
    os.chmod(TOKEN_FILE, 0o600)
    expiry = int(time.time()) + int(t.get("expires_in", 14400)) - 300
    open(EXPIRY_FILE,"w").write(str(expiry))
    mins = t.get("expires_in",0)//60; print(f"[toc_auth] token guardado, expira em {mins} min")

def refresh():
    t = json.load(open(TOKEN_FILE))
    rt = t.get("refresh_token")
    if not rt:
        print("[toc_auth] sem refresh_token"); return False
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, headers={"Content-Type":"application/x-www-form-urlencoded"})
    try:
        r = urllib.request.urlopen(req, timeout=15)
        new_token = json.loads(r.read())
        save_token(new_token)
        return True
    except Exception as e:
        print(f"[toc_auth] refresh falhou: {e}"); return False

def ensure_valid():
    try:
        expiry = int(open(EXPIRY_FILE).read().strip())
        if time.time() < expiry:
            return True
    except: pass
    print("[toc_auth] token expirado ou em falta, a renovar...")
    return refresh()

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        ok = refresh()
        sys.exit(0 if ok else 1)
    ok = ensure_valid()
    print("valid" if ok else "expired")
    sys.exit(0 if ok else 1)
