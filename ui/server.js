const express = require("express");
const path    = require('path');
const fs      = require("fs");
const crypto  = require("crypto");
const { exec } = require("child_process");
const app = express();
app.use(express.static(path.join(__dirname, 'public')));

// --- Carrega ~/.env.db para JWT_SECRET ---
let _JWT_SECRET = 'aios-jwt-secret-2026-change-in-prod';
try {
  const envDb = fs.readFileSync(require('os').homedir() + '/.env.db', 'utf8');
  envDb.split('\n').forEach(line => {
    const [k, v] = line.split('=');
    if (k && v && k.trim() === 'JWT_SECRET') _JWT_SECRET = v.trim();
  });
} catch(e) {}
const JWT_SECRET = _JWT_SECRET;

// --- JWT verification (pure Node.js, sem npm extra) ---
function _b64url(b64) { return b64.replace(/=/g,'').replace(/\+/g,'-').replace(/\//g,'_'); }
function verifyJWT(token) {
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    const [h, p, sig] = parts;
    const expected = _b64url(crypto.createHmac('sha256', JWT_SECRET).update(h+'.'+p).digest('base64'));
    if (sig !== expected) return null;
    const payload = JSON.parse(Buffer.from(p, 'base64').toString('utf8'));
    if (payload.exp && Math.floor(Date.now()/1000) > payload.exp) return null;
    return payload;
  } catch(e) { return null; }
}

app.use(express.json({ limit: '1mb' }));

// --- Rate limiting em memória: 100 req/min por IP ---
const _rl = new Map();
app.use((req, res, next) => {
  const ip  = req.headers['x-forwarded-for']?.split(',')[0]?.trim() || req.socket.remoteAddress || 'unknown';
  const now = Date.now();
  const win = 60_000;
  let   rec = _rl.get(ip);
  if (!rec || now - rec.start > win) { rec = { start: now, count: 0 }; _rl.set(ip, rec); }
  rec.count++;
  if (rec.count > 100) {
    return res.status(429).json({ ok: false, error: 'rate limit exceeded (100 req/min)' });
  }
  next();
});

// --- Auth middleware ---
// NOC endpoints são públicos (dashboard/cluster workers não têm JWT)
// Actions e endpoints de escrita requerem auth
const AUTH_EXEMPT = new Set([
  '/auth/login',
  // NOC read (dashboard)
  '/syshealth', '/telemetry', '/telemetry/history',
  '/backlog/recent', '/jobs/recent', '/watchdog/events',
  '/workers', '/workers/register',
  // Worker pull-model: bypass JWT mas requerem requireWorkerAuth (X-AIOS-WORKER-TOKEN)
  '/worker_jobs/lease', '/worker_jobs/report',
]);
app.use('/api', (req, res, next) => {
  if (AUTH_EXEMPT.has(req.path)) return next();
  const auth  = req.headers['authorization'] || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  if (!token) return res.status(401).json({ ok: false, error: 'unauthorized' });
  const user = verifyJWT(token);
  if (!user) return res.status(401).json({ ok: false, error: 'token inválido ou expirado' });
  req.user = user;
  next();
});

// --- UI -> Agent API proxy ---
// --- Auth endpoints ---
const { execSync } = require('child_process');
app.post('/api/auth/login', (req, res) => {
  try {
    const { username, password } = req.body || {};
    if (!username || !password) return res.status(400).json({ ok: false, error: 'username e password obrigatórios' });
    const safe_u = JSON.stringify(username);
    const safe_p = JSON.stringify(password);
    const out = execSync(`python3 /home/jdl/ai-os/bin/auth.py login ${safe_u} ${safe_p}`, { timeout: 8000 });
    res.json(JSON.parse(out.toString()));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

app.get('/api/users', (req, res) => {
  if (req.user?.role !== 'admin') return res.status(403).json({ ok: false, error: 'apenas admin' });
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/auth.py users', { timeout: 5000 });
    res.json(JSON.parse(out.toString()));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/auth/me', (req, res) => {
  res.json({ ok: true, user: req.user });
});


// ---- OLLAMA PROXY ----
app.post("/api/ollama", async (req,res)=>{
  try{
    const {chatInput} = req.body||{};
    if(!chatInput) return res.status(400).json({ok:false,error:"missing chatInput"});
    const r = await fetch("http://localhost:11434/api/generate",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({model:"qwen2.5-coder:14b",prompt:chatInput,stream:false})
    });
    const j = await r.json();
    return res.json({ok:true,data:{answer:j.response||""}});
  }catch(e){
    return res.status(500).json({ok:false,error:String(e)});
  }
});
// ---- END OLLAMA PROXY ----

app.post("/api/agent", async (req, res) => {
  try {
    const { mode, chatInput } = req.body || {};
    if (!chatInput || typeof chatInput !== "string") {
      return res.status(400).json({ ok: false, error: "Missing chatInput" });
    }
    const payload = {
      mode: typeof mode === "string" ? mode : "openai",
      chatInput
    };

    const r = await fetch("http://localhost:5679/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    const text = await r.text();
    // tenta JSON; se falhar, devolve raw
    try {
      const j = JSON.parse(text);
      return res.status(r.status).json({ ok: r.ok, upstream_status: r.status, data: j });
    } catch {
      return res.status(r.status).json({ ok: r.ok, upstream_status: r.status, raw: text });
    }
  } catch (e) {
    return res.status(500).json({ ok: false, error: String(e) });
  }
});
// --- end proxy ---



app.post("/api/exec", async (req, res) => {
  try {
    const args = (req.body && req.body.args) || null;
    if (!Array.isArray(args) || args.length === 0) {
      return res.status(400).json({ ok:false, error:"args required" });
    }
    const r = await fetch("http://127.0.0.1:8020/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ args })
    });
    const data = await r.json().catch(() => ({}));
    return res.status(r.status).json(data);
  } catch (e) {
    return res.status(500).json({ ok:false, error: String(e) });
  }
});
app.use("/public", require("express").static(__dirname + "/public"));

const HOME = process.env.HOME;
const BACKLOG = HOME + "/ai-os/runtime/backlog.json";
const JOBS = HOME + "/ai-os/runtime/jobs";

app.get("/", (req,res)=> res.sendFile(__dirname + "/index.html"));

app.get("/api/backlog",(req,res)=>{
  try{
    const out = execSync('python3 /home/jdl/ai-os/bin/backlog_pg.py list', {timeout:5000});
    res.json(JSON.parse(out.toString()));
  }catch{ res.json({tasks:[]}); }
});

app.post("/api/add",(req,res)=>{
  try {
    const {goal, title, priority, task_type} = req.body || {};
    const params = JSON.stringify({
      goal: goal||'', title: title||goal||'',
      priority: priority||5, task_type: task_type||'DEV_TASK'
    });
    const out = execSync(`python3 /home/jdl/ai-os/bin/backlog_pg.py add ${JSON.stringify(params)}`, {timeout:5000});
    res.json(JSON.parse(out.toString()));
  } catch(e) { res.json({ok:false, error:String(e)}); }
});

app.post("/api/run",(req,res)=>{
  exec(HOME + "/ai-os/bin/autopilot_worker.sh");
  res.json({ok:true});
});

app.get("/api/status",(req,res)=>{
  try{
    const out = execSync('python3 /home/jdl/ai-os/bin/backlog_pg.py status', {timeout:5000});
    res.json(JSON.parse(out.toString()));
  }catch{ res.json({status:"UNKNOWN"}); }
});

app.get("/api/lastjob",(req,res)=>{
  try{
    const dirs=fs.readdirSync(JOBS).sort().reverse();
    if(!dirs.length) return res.json({});
    const jid=dirs[0];
    const result=JSON.parse(fs.readFileSync(JOBS+"/"+jid+"/result.json"));
    res.json({job:jid, result});
  }catch{ res.json({}); }
});




// últimos logs do agent-router
app.get("/api/logs",(req,res)=>{
  const { exec } = require("child_process");
  exec("docker logs agent-router --tail 100", (err,stdout,stderr)=>{
    res.json({logs: stdout || stderr});
  });
});

// Finance routes — Toconline
app.get('/api/finance/customers', (req,res)=>{
  try{
    const limit = req.query.limit || 10;
    const out = execSync('python3 /home/jdl/ai-os/bin/tools_finance.py toc_customers ' + JSON.stringify(JSON.stringify({limit:parseInt(limit)})), {timeout:10000});
    res.json(JSON.parse(out.toString()));
  }catch(e){ res.json({ok:false,error:String(e)}); }
});
app.get('/api/finance/invoices', (req,res)=>{
  try{
    const limit = req.query.limit || 10;
    const out = execSync('python3 /home/jdl/ai-os/bin/tools_finance.py toc_invoices ' + JSON.stringify(JSON.stringify({limit:parseInt(limit)})), {timeout:10000});
    res.json(JSON.parse(out.toString()));
  }catch(e){ res.json({ok:false,error:String(e)}); }
});

app.post('/api/mode',(req,res)=>res.json({ok:true}));

// Factory / Modbus sensors
app.get('/api/sensors', (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/tools_factory.py factory_status {}', { timeout: 5000 });
    res.json(JSON.parse(out.toString()));
  } catch (e) { res.json({ ok: false, error: String(e) }); }
});

// DMX / Art-Net state (lê directamente o JSON — sem dependência do simulador)
const DMX_STATE = HOME + '/ai-os/runtime/dmx_state.json';
app.get('/api/dmx', (req, res) => {
  try {
    const raw  = JSON.parse(fs.readFileSync(DMX_STATE));
    const uni  = raw.universes && raw.universes['0'] ? raw.universes['0'] : Array(512).fill(0);
    res.json({
      ok: true,
      R:      uni[0] || 0,
      G:      uni[1] || 0,
      B:      uni[2] || 0,
      dimmer: uni[3] || 0,
      ch1_8:  uni.slice(0, 8),
      active: uni.some(v => v > 0),
      ts:     raw.ts || 0,
    });
  } catch { res.json({ ok: false, R:0, G:0, B:0, dimmer:0, active:false }); }
});
app.post('/api/dmx/scene', (req, res) => {
  try {
    const { scene } = req.body || {};
    if (!scene) return res.status(400).json({ ok: false, error: 'scene required' });
    const out = execSync(`python3 /home/jdl/ai-os/bin/tools_dmx.py dmx_scene ${JSON.stringify(JSON.stringify({ scene }))}`, { timeout: 5000 });
    res.json(JSON.parse(out.toString()));
  } catch (e) { res.json({ ok: false, error: String(e) }); }
});

// Approvals
app.get('/api/approvals', (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/approval_pg.py list', {timeout:5000});
    res.json(JSON.parse(out.toString()));
  } catch { res.json({ approvals: [] }); }
});
app.post('/api/approve', (req, res) => {
  try {
    const { id } = req.body || {};
    if (!id) return res.status(400).json({ ok: false, error: 'id required' });
    const uid = req.user?.sub || '';
    const out = execSync(`python3 /home/jdl/ai-os/bin/approval_pg.py approve ${JSON.stringify(id)} ${uid}`, {timeout:5000});
    res.json(JSON.parse(out.toString()));
  } catch (e) { res.status(500).json({ ok: false, error: String(e) }); }
});
app.post('/api/reject', (req, res) => {
  try {
    const { id } = req.body || {};
    if (!id) return res.status(400).json({ ok: false, error: 'id required' });
    const uid = req.user?.sub || '';
    const out = execSync(`python3 /home/jdl/ai-os/bin/approval_pg.py reject ${JSON.stringify(id)} ${uid}`, {timeout:5000});
    res.json(JSON.parse(out.toString()));
  } catch (e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// ── NOC endpoints ─────────────────────────────────────────────────────────────

const AIOS_ROOT = process.env.AIOS_ROOT || require('os').homedir() + '/ai-os';
const NOC_PY = `python3 ${AIOS_ROOT}/bin/noc_query.py`;
const DB_ENV = `DATABASE_URL=${process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1:5432/aios'}`;

// ---- Worker auth: valida X-AIOS-WORKER-ID + X-AIOS-WORKER-TOKEN contra DB ----
function requireWorkerAuth(req, res, next) {
  const wid = String(req.headers['x-aios-worker-id'] || '').trim();
  const tok = String(req.headers['x-aios-worker-token'] || '').trim();
  if (!wid || !tok) return res.status(403).json({ ok: false, error: 'worker_auth_missing' });
  // apenas alfanumérico + hifens (ex: DESKTOP-CPLTTV3-agent)
  if (!/^[\w\-]{1,64}$/.test(wid)) return res.status(403).json({ ok: false, error: 'worker_id_invalid' });
  try {
    const DB_URL = process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1:5432/aios';
    const NOC    = `${require('os').homedir()}/ai-os/bin/noc_query.py`;
    const out    = execSync(
      `DATABASE_URL=${DB_URL} python3 ${NOC} worker_token_check ${wid}`,
      { timeout: 5000, encoding: 'utf8' }
    );
    const dbtok  = (JSON.parse(out).token || '');
    if (!dbtok || dbtok !== tok) return res.status(403).json({ ok: false, error: 'worker_auth_denied' });
    req.worker_id = wid;
    next();
  } catch (e) {
    return res.status(500).json({ ok: false, error: 'worker_auth_error' });
  }
}

function nocExec(cmd, timeout = 8000) {
  try {
    const out = execSync(`${DB_ENV} ${NOC_PY} ${cmd}`, { timeout, encoding: 'utf8' });
    return JSON.parse(out);
  } catch (e) {
    return { error: (e.stderr || e.message || String(e)).slice(0, 300) };
  }
}

// Serve ops.html
app.get('/ops', (req, res) => res.sendFile(__dirname + '/ops.html'));

// System health: containers + timers + backlog
app.get('/api/syshealth', (req, res) => res.json(nocExec('syshealth')));

// Telemetria live (última leitura por host)
app.get('/api/telemetry', (req, res) => res.json(nocExec('telemetry_live')));

// Histórico de telemetria
app.get('/api/telemetry/history', (req, res) => {
  const n    = parseInt(req.query.n || '120', 10);
  const host = req.query.host ? ` ${req.query.host}` : '';
  res.json(nocExec(`telemetry_history ${n}${host}`));
});

// Backlog recente
app.get('/api/backlog/recent', (req, res) => {
  const limit = parseInt(req.query.limit || '20', 10);
  res.json(nocExec(`backlog_recent ${limit}`));
});

// Jobs filesystem (últimos job dirs)
app.get('/api/jobs/recent', (req, res) => {
  const limit = parseInt(req.query.limit || '15', 10);
  try {
    const jobsDir = AIOS_ROOT + '/runtime/jobs';
    const dirs = require('fs').readdirSync(jobsDir)
      .filter(d => /^\d{8}_\d{6}_/.test(d))
      .sort().reverse().slice(0, limit)
      .map(d => {
        try {
          const finalPath = `${jobsDir}/${d}/final.json`;
          const final = require('fs').existsSync(finalPath)
            ? JSON.parse(require('fs').readFileSync(finalPath, 'utf8')) : {};
          const goalPath = `${jobsDir}/${d}/goal.txt`;
          const goal = require('fs').existsSync(goalPath)
            ? require('fs').readFileSync(goalPath, 'utf8').trim().slice(0, 80) : '';
          return { job_id: d, ok: final.ok, rounds: final.rounds, error: final.error, goal };
        } catch { return { job_id: d }; }
      });
    res.json(dirs);
  } catch (e) {
    res.json({ error: e.message });
  }
});

// Eventos
app.get('/api/watchdog/events', (req, res) => {
  const n = parseInt(req.query.n || '30', 10);
  res.json(nocExec(`events ${n}`));
});

// Workers
app.get('/api/workers', (req, res) => res.json(nocExec('workers')));

// Registo de worker (GET ou POST)
app.get('/api/workers/register', (req, res) => {
  const { id, hostname, role } = req.query;
  if (!id || !hostname || !role) return res.status(400).json({ ok: false, error: 'id, hostname, role required' });
  res.json(nocExec(`worker_register ${id} ${hostname} ${role}`));
});
app.post('/api/workers/register', (req, res) => {
  const { id, hostname, role } = { ...req.query, ...req.body };
  if (!id || !hostname || !role) return res.status(400).json({ ok: false, error: 'id, hostname, role required' });
  res.json(nocExec(`worker_register ${id} ${hostname} ${role}`));
});

// Worker jobs
app.get('/api/worker_jobs', (req, res) => {
  const limit = parseInt(req.query.limit || '30', 10);
  res.json(nocExec(`worker_jobs ${limit}`));
});

app.get('/api/worker_jobs/lease', requireWorkerAuth, (req, res) => {
  const wid = req.worker_id || String(req.query.worker_id || '');
  if (!wid) return res.status(400).json({ ok: false, error: 'worker_id required' });
  res.json(nocExec(`worker_jobs_lease ${wid}`));
});

app.post('/api/worker_jobs/report', requireWorkerAuth, (req, res) => {
  const { job_id, status, result } = req.body || {};
  if (!job_id || !status) return res.status(400).json({ ok: false, error: 'job_id, status required' });
  const resultJson = JSON.stringify(result || {}).replace(/'/g, '"');
  res.json(nocExec(`worker_jobs_report ${job_id} ${status} '${resultJson}'`));
});

app.post('/api/worker_jobs/enqueue', (req, res) => {
  // Auth obrigatório (está fora do AUTH_EXEMPT)
  const { kind, payload, target_worker_id } = req.body || {};
  if (!kind) return res.status(400).json({ ok: false, error: 'kind required' });
  try {
    const DB_URL = process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1:5432/aios';
    const py = `
import json, os
import sqlalchemy as sa
engine = sa.create_engine(${JSON.stringify(DB_URL)})
with engine.begin() as c:
    r = c.execute(sa.text(
        "INSERT INTO public.worker_jobs (ts_created, status, kind, payload, target_worker_id) "
        "VALUES (NOW(), 'queued', :kind, :payload, :target) RETURNING id"
    ), {"kind": ${JSON.stringify(kind)}, "payload": json.dumps(${JSON.stringify(payload || {})}), "target": ${JSON.stringify(target_worker_id || null)}})
    print(r.scalar())
`;
    const out = execSync(`python3 -c "${py.replace(/\n/g, '; ')}"`, { timeout: 5000, encoding: 'utf8' }).trim();
    res.json({ ok: true, job_id: parseInt(out, 10) });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message.slice(0, 200) });
  }
});

// Acções de controlo (requerem auth — não estão em AUTH_EXEMPT)
app.post('/api/actions/tick', (req, res) => {
  exec(`sudo systemctl start aios-autopilot.service 2>&1 || python3 ${AIOS_ROOT}/bin/autopilot_tick.py`, (err, out) => {
    res.json({ ok: !err, output: (out || '').slice(0, 200) });
  });
});

app.post('/api/actions/watchdog', (req, res) => {
  exec(`sudo systemctl start aios-watchdog.service 2>&1`, (err, out) => {
    res.json({ ok: !err, output: (out || '').slice(0, 200) });
  });
});

app.post('/api/actions/healthcheck', (req, res) => {
  exec(`${AIOS_ROOT}/bin/aios_health.sh 2>&1`, { timeout: 10000 }, (err, out) => {
    res.json({ ok: !err, output: (out || '').slice(0, 1000) });
  });
});

// Retenção: elimina dados antigos (>N dias) — chamado manualmente ou por timer
app.post('/api/maintenance/cleanup', (req, res) => {
  const days = parseInt(req.body?.days || '30', 10);
  try {
    const out = execSync(`${DB_ENV} python3 -c "
import sqlalchemy as sa, os
engine = sa.create_engine(os.environ['DATABASE_URL'])
with engine.begin() as c:
    t=c.execute(sa.text('DELETE FROM public.telemetry WHERE ts < NOW() - INTERVAL \\':days days\\''), {'days': ${days}}).rowcount
    e=c.execute(sa.text('DELETE FROM public.events WHERE ts < NOW() - INTERVAL \\':days days\\''), {'days': ${days}}).rowcount
    w=c.execute(sa.text('DELETE FROM public.worker_jobs WHERE ts_created < NOW() - INTERVAL \\':days days\\''), {'days': ${days}}).rowcount
    print(t, e, w)
"`, { timeout: 15000, encoding: 'utf8' }).trim().split(' ');
    res.json({ ok: true, deleted: { telemetry: +out[0], events: +out[1], worker_jobs: +out[2] } });
  } catch (e) {
    res.status(500).json({ ok: false, error: e.message.slice(0, 200) });
  }
});

// ── versão e boot ─────────────────────────────────────────────────────────────

app.get('/api/version', (req, res) => res.json({ version: '1.0.0', name: 'ai-os' }));

app.listen(3000, () => console.log("UI http://localhost:3000"));
