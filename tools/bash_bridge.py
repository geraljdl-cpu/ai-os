"""
bash_bridge.py — executa comandos via HTTP (POST /run)
Payload: {"cmd": ["bash", "-lc", "..."], "timeout": 30}
Response: {"stdout": "...", "stderr": "...", "returncode": 0}
"""
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

PORT = 8020


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # silencia logs de acesso

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/run":
            self._json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "invalid json"})
            return

        cmd = data.get("cmd")
        timeout = data.get("timeout", 60)

        if not cmd or not isinstance(cmd, list):
            self._json(400, {"error": "cmd must be a non-empty list"})
            return

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            self._json(200, {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            })
        except subprocess.TimeoutExpired:
            self._json(200, {"stdout": "", "stderr": "timeout", "returncode": -1})
        except Exception as e:
            self._json(500, {"error": str(e)})

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"bash-bridge listening on :{PORT}", flush=True)
    server.serve_forever()
