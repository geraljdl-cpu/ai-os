#!/usr/bin/env python3
import json, os, subprocess, datetime, pathlib, textwrap, sys, urllib.request

REPO = os.environ.get("AIOS_REPO", os.path.expanduser("~/ai-os"))
AGENT = os.environ.get("AIOS_AGENT_URL", "http://127.0.0.1:5679/agent")
MODE  = os.environ.get("AIOS_MODE", "ollama")  # default teu
OUTD  = pathlib.Path(os.environ.get("AIOS_OUT", os.path.expanduser("~/ai-os/runtime/autopilot")))
OUTD.mkdir(parents=True, exist_ok=True)

SAFE_CMDS = [
  ["git","status","--porcelain=v1"],
  ["git","rev-parse","--abbrev-ref","HEAD"],
  ["git","log","-1","--oneline","--decorate"],
  ["git","diff","--stat"],
  ["ls","-la"],
]

def run(cmd):
  try:
    r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=20)
    return {"cmd":" ".join(cmd), "code":r.returncode, "out":r.stdout.strip(), "err":r.stderr.strip()}
  except Exception as e:
    return {"cmd":" ".join(cmd), "code":999, "out":"", "err":str(e)}

def http_post(url, payload):
  data = json.dumps(payload).encode("utf-8")
  req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
  with urllib.request.urlopen(req, timeout=120) as resp:
    return resp.read().decode("utf-8", errors="replace")

def main():
  ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
  report = OUTD / f"{ts}_report.md"
  diffp  = OUTD / f"{ts}_patch.diff"
  rawp   = OUTD / f"{ts}_agent_raw.json"

  snaps = [run(c) for c in SAFE_CMDS]

  prompt = f"""
Contexto: Estou a construir um sistema local (UI + exec bridge) e quero avançar o "autopilot".
Repo: {REPO}

Estado atual (snapshot de comandos):
{json.dumps(snaps, ensure_ascii=False, indent=2)}

Tarefa (IMPORTANTE):
- NÃO executar comandos nem assumir acesso ao sistema.
- Produz um plano curto (máx 12 linhas) para o próximo passo AUTOPILOT.
- Produz um unified diff (patch) com mudanças concretas e pequenas (idealmente: integrar /api/exec no server.js ou criar um endpoint no agent-router para tool exec).
- O patch tem de ser aplicável em git (diff unificado).
- Se não conseguires gerar patch com confiança, devolve patch vazio e explica o que faltou.

Formato de resposta: JSON com chaves:
{{
  "plan": ["..."],
  "diff": ".... (unified diff ou vazio)",
  "notes": "..."
}}
""".strip()

  resp_txt = http_post(AGENT, {"chatInput": prompt, "mode": MODE})
  # agent-router às vezes devolve JSON wrapper; guardamos bruto e tentamos extrair
  rawp.write_text(resp_txt, encoding="utf-8")

  plan = []
  diff = ""
  notes = ""

  try:
    obj = json.loads(resp_txt)
    # wrapper esperado: {"status":"ok","answer":"..."} ou direto
    if isinstance(obj, dict) and "answer" in obj and isinstance(obj["answer"], str):
      inner = obj["answer"].strip()
      try:
        obj2 = json.loads(inner)
      except:
        obj2 = None
      if isinstance(obj2, dict):
        plan = obj2.get("plan") or []
        diff = obj2.get("diff") or ""
        notes = obj2.get("notes") or ""
      else:
        notes = inner
    elif isinstance(obj, dict):
      plan = obj.get("plan") or []
      diff = obj.get("diff") or ""
      notes = obj.get("notes") or ""
  except Exception as e:
    notes = f"Failed to parse JSON: {e}\n\nRaw:\n{resp_txt[:2000]}"

  if diff.strip():
    diffp.write_text(diff, encoding="utf-8")
  else:
    diffp.write_text("", encoding="utf-8")

  md = []
  md.append(f"# Nightly Planner {ts}")
  md.append(f"- mode: {MODE}")
  md.append(f"- repo: {REPO}")
  md.append("")
  md.append("## Snapshot")
  for s in snaps:
    md.append(f"### `{s['cmd']}` (code {s['code']})")
    if s["out"]:
      md.append("```")
      md.append(s["out"])
      md.append("```")
    if s["err"]:
      md.append("ERR:")
      md.append("```")
      md.append(s["err"])
      md.append("```")
  md.append("")
  md.append("## Plan")
  if isinstance(plan, list) and plan:
    for i,x in enumerate(plan,1):
      md.append(f"{i}. {x}")
  else:
    md.append("(no plan parsed)")
  md.append("")
  md.append("## Notes")
  md.append(notes or "")
  md.append("")
  md.append(f"## Patch file\n- {diffp}")
  report.write_text("\n".join(md), encoding="utf-8")

  print(str(report))
  print(str(diffp))

if __name__ == "__main__":
  main()
