#!/usr/bin/env python3
import json, subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST="0.0.0.0"
PORT=8020
CWD="/host"
ALLOW={"pwd","ls","whoami","id","uname","date","uptime","df","du","free","ps","ss","ip","cat","head","tail","grep","find","wc","echo"}

class H(BaseHTTPRequestHandler):
  def log_message(self, *args): return
  def j(self, code, obj):
    try:
      b=json.dumps(obj).encode("utf-8")
      self.send_response(code)
      self.send_header("Content-Type","application/json")
      self.send_header("Content-Length", str(len(b)))
      self.end_headers()
      self.wfile.write(b)
    except (BrokenPipeError, ConnectionResetError):
      return

  def do_GET(self):
    if self.path=="/health": return self.j(200, {"ok":True})
    return self.j(404, {"ok":False})

  def do_POST(self):
    if self.path!="/run": return self.j(404, {"ok":False})
    try:
      n=int(self.headers.get("Content-Length","0"))
      data=json.loads(self.rfile.read(n) or b"{}")
      args=data.get("args")
      if not isinstance(args, list) or not args: return self.j(400, {"ok":False,"error":"args required"})
      args=[str(x) for x in args]
      if args[0] not in ALLOW: return self.j(403, {"ok":False,"error":"not allowed"})
      r=subprocess.run(args, capture_output=True, text=True, timeout=12, cwd=CWD)
      return self.j(200, {"ok":True,"args":args,"code":r.returncode,"stdout":r.stdout,"stderr":r.stderr})
    except Exception as e:
      return self.j(500, {"ok":False,"error":str(e)})

if __name__=="__main__":
  print(f"bash-bridge on :{PORT} cwd={CWD}")
  HTTPServer((HOST,PORT), H).serve_forever()
