#!/usr/bin/env python3
"""
test_whatsapp.py — Simula webhook Twilio para testar o módulo de ponto

Uso:
  python3 bin/test_whatsapp.py                   # flow completo
  python3 bin/test_whatsapp.py --phone +351XXXXX  # com número específico
  python3 bin/test_whatsapp.py --direct           # chama handler directamente (sem HTTP)
"""
import argparse, json, os, subprocess, sys

BASE         = os.environ.get("AIOS_UI_BASE", "http://localhost:3000")
TEST_PHONE   = "+351910000001"
TEST_NAME    = "Colaborador Teste"


def via_http(phone: str, body: str, lat=None, lon=None, addr=None):
    """Simula POST do Twilio para /api/whatsapp/inbound."""
    import urllib.request, urllib.parse
    data = {"From": f"whatsapp:{phone}", "To": "whatsapp:+14155238886", "Body": body}
    if lat:
        data["Latitude"]  = str(lat)
        data["Longitude"] = str(lon)
    if addr:
        data["Address"] = addr

    payload = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"{BASE}/api/whatsapp/inbound",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode()
    except Exception as e:
        return f"HTTP ERROR: {e}"


def via_direct(phone: str, body: str, lat=None, lon=None, addr=None):
    """Chama bin/whatsapp_handler.py directamente."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "whatsapp_handler.py")
    args = ["python3", script, "--from", phone, "--body", body]
    if lat:
        args += ["--lat", str(lat), "--lon", str(lon)]
    if addr:
        args += ["--addr", addr]
    r = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return f"ERROR: {r.stderr[:300]}"
    try:
        d = json.loads(r.stdout)
        return d.get("reply", r.stdout)
    except Exception:
        return r.stdout


def run_test(send_fn, phone: str):
    def t(label, body, **kw):
        print(f"\n{'─'*60}")
        print(f">>> [{label}] {phone}: {body!r}")
        reply = send_fn(phone, body, **kw)
        print(f"    {reply}")

    # 1. Registo
    t("registo",    f"registo {TEST_NAME}")
    # 2. Ajuda
    t("ajuda",      "ajuda")
    # 3. Inicio sem local (deve pedir local)
    t("inicio vazio", "inicio")
    # 4. Inicio com local
    t("inicio",     "inicio Lisboa")
    # 5. GPS
    t("gps",        "", lat=38.7223, lon=-9.1393, addr="Rua Augusta, Lisboa")
    # 6. Estado
    t("estado",     "estado")
    # 7. Duplo inicio (deve bloquear)
    t("duplo inicio", "inicio Porto")
    # 8. Fim
    t("fim",        "fim")
    # 9. Estado pós-fim
    t("estado final", "estado")
    # 10. Fim sem turno (deve dar erro amigável)
    t("fim sem turno", "fim")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone",  default=TEST_PHONE)
    parser.add_argument("--direct", action="store_true",
                        help="Chamar handler directamente (sem HTTP)")
    args = parser.parse_args()

    print(f"{'='*60}")
    print(f" WhatsApp Ponto — Teste {'directo' if args.direct else 'HTTP'}")
    print(f" Phone: {args.phone}")
    print(f" Base:  {BASE}")
    print(f"{'='*60}")

    fn = via_direct if args.direct else via_http
    run_test(fn, args.phone)

    print(f"\n{'='*60}")
    print(" Teste concluído. Ver logs em runtime/whatsapp/handler.log")


if __name__ == "__main__":
    main()
