#!/usr/bin/env python3
"""
at_session.py — Portal das Finanças (AT) session manager

Flow:
  1. GET acesso.gov.pt/v2/loginForm → extract CSRF token
  2. POST /v2/login → SSO form with signed token
  3. POST sitfiscal.portaldasfinancas.gov.pt/geral/dashboard → session active

Usage:
  from at_session import ATSession
  s = ATSession()
  ok = s.login()           # True/False
  html = s.get("/pt/home") # fetches authenticated page
"""

import re, ssl, http.cookiejar, urllib.request, urllib.parse, os

_env = "/etc/aios.env"
if os.path.exists(_env):
    for _l in open(_env):
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            k, _, v = _l.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

def _sysconfig(key: str) -> str:
    """Read from system_config table."""
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
        dsn = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
        with psycopg2.connect(dsn, cursor_factory=RealDictCursor) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT value FROM public.system_config WHERE key=%s", (key,))
                row = cur.fetchone()
                return row["value"] if row else ""
    except Exception:
        return ""


# Known sub-service partIDs
SERVICE_PARTS = {
    "portal":            ("PFAP",  "https://sitfiscal.portaldasfinancas.gov.pt/geral/dashboard",          "/pt/home"),
    "dividas":           ("CDEF",  "https://justica.portaldasfinancas.gov.pt/consdivsef/dividasVoluntarias/listaDividas", "/consdivsef/dividasVoluntarias/listaDividas"),
    "executivos":        ("CPEE",  "https://justica.portaldasfinancas.gov.pt/consentext/processosExecutivos/pesquisaProcessosExecutivos", "/consentext/processosExecutivos/pesquisaProcessosExecutivos"),
    "compensacao":       ("SGAC",  "https://processos.portaldasfinancas.gov.pt/compdividas/contribuinte/consultar", "/compdividas/contribuinte/consultar"),
    "cobranca":          ("DIFC",  "https://sitfiscal.portaldasfinancas.gov.pt/movfin/resumoCobranca",    "/movfin/resumoCobranca"),
    "prestacional":      ("SGPP",  "https://sitfiscal.portaldasfinancas.gov.pt/planosprestacionais/registarsimular/listaNotasCobranca", "/planosprestacionais/registarsimular/listaNotasCobranca"),
    "irs":               ("IRS",   "https://irs.portaldasfinancas.gov.pt/app/dashboard-regime-simplificado", "/app/dashboard-regime-simplificado"),
    "divergencias":      ("DIVEQ", "https://irs.portaldasfinancas.gov.pt/divergencias/consultarAlertas",  "/divergencias/consultarAlertas"),
}


class ATSession:
    LOGIN_FORM  = "https://www.acesso.gov.pt/v2/loginForm?partID={partID}&path={path}"
    LOGIN_POST  = "https://www.acesso.gov.pt/v2/login"
    PORTAL_BASE = "https://sitfiscal.portaldasfinancas.gov.pt"

    def __init__(self, nif: str = "", password: str = ""):
        self.nif      = nif      or _sysconfig("at_nif_pessoal")
        self.password = password or _sysconfig("at_password_pessoal")
        self._sessions: dict[str, dict] = {}   # partID → {jar, opener}

        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode    = ssl.CERT_NONE

        # Default session (PFAP portal)
        self._jar, self._opener = self._make_opener()
        self._logged_in = False

    def _make_opener(self):
        jar    = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ctx),
            urllib.request.HTTPCookieProcessor(jar),
        )
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"),
        ]
        return jar, opener

    # ── helpers ───────────────────────────────────────────────────────────────

    def _fetch(self, url: str, data: bytes = None, headers: dict = None) -> str:
        req = urllib.request.Request(url, data=data)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        r = self._opener.open(req, timeout=20)
        return r.read().decode("utf-8", errors="ignore"), r.url

    # ── login ─────────────────────────────────────────────────────────────────

    def _do_login(self, part_id: str, path: str, target_url: str, jar, opener) -> bool:
        """Generic SSO login for any partID. Returns True on success."""
        login_form = self.LOGIN_FORM.format(partID=part_id, path=urllib.parse.quote(path))

        def fetch(url, data=None, headers=None):
            req = urllib.request.Request(url, data=data)
            for k, v in (headers or {}).items():
                req.add_header(k, v)
            r = opener.open(req, timeout=20)
            return r.read().decode("utf-8", errors="ignore"), r.url

        # Step 1: CSRF
        html, _ = fetch(login_form)
        m = re.search(r"token:\s*`([a-f0-9\-]{36})`", html)
        if not m:
            return False
        csrf = m.group(1)

        # Step 2: POST credentials
        post_data = urllib.parse.urlencode({
            "username": self.nif, "password": self.password,
            "_csrf": csrf, "selectedAuthMethod": "N",
        }).encode()
        html2, _ = fetch(self.LOGIN_POST, data=post_data, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": login_form, "Origin": "https://www.acesso.gov.pt",
        })
        if "loginSuccess: parseBoolean('true')" not in html2:
            return False

        # Step 3: SSO POST to target
        hidden = re.findall(
            r'<input[^>]+type=["\']hidden["\'][^>]*name=["\']([^"\']+)["\'][^>]*value=["\']([^"\']*)["\']',
            html2,
        )
        action_m = re.search(r"action:\s*stringOrNull\('([^']+)'\)", html2)
        dest = action_m.group(1) if action_m else target_url
        _, final = fetch(dest, data=urllib.parse.urlencode(dict(hidden)).encode(), headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.acesso.gov.pt/", "Origin": "https://www.acesso.gov.pt",
        })
        return "portaldasfinancas" in final

    def login(self) -> bool:
        """Login to main portal (PFAP)."""
        if not self.nif or not self.password:
            print("[at_session] Credenciais não configuradas")
            return False
        ok = self._do_login("PFAP", "/pt/home",
                            f"{self.PORTAL_BASE}/geral/dashboard",
                            self._jar, self._opener)
        self._logged_in = ok
        if ok:
            print(f"[at_session] ✅ Portal das Finanças — sessão activa")
        else:
            print(f"[at_session] ❌ Login falhou")
        return ok

    def login_service(self, service: str) -> tuple:
        """Login to a specific AT sub-service. Returns (opener, final_url)."""
        if service not in SERVICE_PARTS:
            raise ValueError(f"Serviço desconhecido: {service}. Disponíveis: {list(SERVICE_PARTS)}")
        part_id, target_url, path = SERVICE_PARTS[service]

        if service in self._sessions:
            return self._sessions[service]["opener"]

        jar, opener = self._make_opener()
        ok = self._do_login(part_id, path, target_url, jar, opener)
        if ok:
            self._sessions[service] = {"jar": jar, "opener": opener}
            print(f"[at_session] ✅ {service} ({part_id}) — sessão activa")
            return opener
        else:
            print(f"[at_session] ❌ {service} ({part_id}) — falhou")
            return None

    def fetch_service(self, service: str, url: str) -> str:
        """Fetch an authenticated URL for a specific sub-service."""
        opener = self.login_service(service)
        if not opener:
            return ""
        req = urllib.request.Request(url)
        r   = opener.open(req, timeout=20)
        return r.read().decode("utf-8", errors="ignore")

    # ── portal requests ───────────────────────────────────────────────────────

    def get(self, path: str) -> str:
        """Fetch an authenticated AT portal page."""
        if not self._logged_in:
            self.login()
        url = f"{self.PORTAL_BASE}{path}" if path.startswith("/") else path
        html, _ = self._fetch(url)
        return html

    def portal_home(self) -> dict:
        """Return basic info from the portal home page."""
        html = self.get("/geral/home")
        name_m = re.search(r'(?:Bem.vindo|olá)[,\s]+([^<\n]{3,60})', html, re.I)
        nif_m  = re.search(r'NIF[:\s]+(\d{9})', html)
        return {
            "logged_in": self._logged_in,
            "name":      name_m.group(1).strip() if name_m else None,
            "nif":       nif_m.group(1) if nif_m else self.nif,
            "url":       f"{self.PORTAL_BASE}/geral/home",
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json as _json

    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    s   = ATSession()

    if cmd == "login":
        ok = s.login()
        print("ok" if ok else "falhou")
        sys.exit(0 if ok else 1)

    elif cmd == "status":
        ok = s.login()
        info = s.portal_home()
        print(_json.dumps(info, ensure_ascii=False))
        sys.exit(0 if ok else 1)

    elif cmd == "get":
        path = sys.argv[2] if len(sys.argv) > 2 else "/geral/home"
        s.login()
        print(s.get(path)[:3000])

    else:
        print("Usage: at_session.py login|status|get [/path]")
        sys.exit(1)
