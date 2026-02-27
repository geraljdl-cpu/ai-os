# AI-OS

Stack local para correr agent-router, backlog e autopilot worker.

## Arrancar stack
cd ~/ai-os
docker compose up -d
docker ps
curl -sS http://127.0.0.1:5679/health

## Enfileirar task
python3 - <<'PY'
import json, os
p=os.path.expanduser("~/ai-os/runtime/backlog.json")
d=json.load(open(p))
d["tasks"].append({"goal":"EXEMPLO: criar ficheiro teste"})
json.dump(d, open(p,"w"), indent=2, ensure_ascii=False)
print("QUEUED_OK")
PY

bash ~/ai-os/bin/autopilot_worker.sh
ls -1t ~/ai-os/runtime/jobs | head -n1

## Ver resultado do job
jid=$(ls -1t ~/ai-os/runtime/jobs | head -n1)
sed -n '1,200p' ~/ai-os/runtime/jobs/$jid/agent_response.json
sed -n '1,200p' ~/ai-os/runtime/jobs/$jid/patch.diff
tail -n 60 ~/ai-os/runtime/jobs/$jid/log.txt

## Merge manual
git branch
git checkout main
git merge --no-ff aios/<JOB_ID>
