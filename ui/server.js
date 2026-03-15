const express = require("express");
const path    = require('path');
const fs      = require("fs");
const crypto  = require("crypto");
const { exec } = require("child_process");
const app = express();
app.use(express.static(path.join(__dirname, 'public')));

// --- Carrega ~/.env.db e /etc/aios.env ---
let _JWT_SECRET = 'aios-jwt-secret-2026-change-in-prod';
let _OPS_TOKEN  = '';
let _ANTHROPIC_KEY = '';
function _loadEnvFile(path) {
  try {
    fs.readFileSync(path, 'utf8').split('\n').forEach(line => {
      const eq = line.indexOf('=');
      if (eq < 1) return;
      const k = line.slice(0, eq).trim();
      const v = line.slice(eq + 1).trim();
      if (k === 'JWT_SECRET')        _JWT_SECRET    = v;
      if (k === 'AIOS_OPS_TOKEN')    _OPS_TOKEN     = v;
      if (k === 'ANTHROPIC_API_KEY') _ANTHROPIC_KEY = v;
    });
  } catch(e) {}
}
_loadEnvFile(require('os').homedir() + '/.env.db');
_loadEnvFile('/etc/aios.env');
const JWT_SECRET    = _JWT_SECRET;
const OPS_TOKEN     = _OPS_TOKEN || process.env.AIOS_OPS_TOKEN || '';
const ANTHROPIC_KEY = _ANTHROPIC_KEY || process.env.ANTHROPIC_API_KEY || '';

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
  '/twin/approvals', '/twin/cases', '/twin/cable_batch',
  '/twin/batch', '/twin/batch/by-token',
  '/actions/tick', '/actions/watchdog', '/actions/healthcheck',
  // Worker pull-model: bypass JWT mas requerem requireWorkerAuth (X-AIOS-WORKER-TOKEN)
  '/worker_jobs/lease', '/worker_jobs/report',
  // Enqueue: bypass JWT mas requer X-AIOS-OPS-TOKEN (ou JWT)
  '/worker_jobs/enqueue',
  // Control room — leitura pública (TV wall)
  '/control/overview',
  '/agent/status',
  '/incidents',
  // Toconline health — public (sem dados sensíveis)
  '/finance/toconline/health',
]);
app.use('/api', (req, res, next) => {
  if (AUTH_EXEMPT.has(req.path)) return next();
  if (req.path.startsWith('/twin/batch'))  return next();  // auth própria em cada endpoint
  if (req.path.startsWith('/twin/client')) return next(); // client portal — token validado no noc_query
  if (req.path.match(/^\/client\/[a-zA-Z0-9]+\/(timesheets|timesheet)/)) return next(); // client timesheet portal
  // OPS token bypasses JWT entirely (system/bot calls)
  const opsTokGlobal = String(req.headers['x-aios-ops-token'] || '').trim();
  if (OPS_TOKEN && opsTokGlobal === OPS_TOKEN) return next();
  const auth  = req.headers['authorization'] || '';
  const token = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  if (!token) return res.status(401).json({ ok: false, error: 'unauthorized' });
  const user = verifyJWT(token);
  if (!user) return res.status(401).json({ ok: false, error: 'token inválido ou expirado' });
  req.user = user;
  next();
});

// --- requireRole middleware ---
// RBAC: admin > supervisor > operator/factory > finance > viewer > show > cliente
const ROLE_LEVEL = { admin:100, supervisor:80, operator:60, factory:60, finance:50, viewer:30, worker:25, show:20, cliente:10 };
function requireRole(...roles) {
  return (req, res, next) => {
    // OPS token bypasses role check (system/bot calls)
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (OPS_TOKEN && opsTok === OPS_TOKEN) return next();
    if (!req.user) return res.status(401).json({ ok: false, error: 'unauthorized' });
    if (roles.includes(req.user.role)) return next();
    // Also allow admin/supervisor for any role-protected endpoint
    const userLevel  = ROLE_LEVEL[req.user.role] || 0;
    const minLevel   = Math.min(...roles.map(r => ROLE_LEVEL[r] || 999));
    if (userLevel >= 80) return next(); // admin/supervisor pass everything
    if (userLevel >= minLevel) return next();
    return res.status(403).json({ ok: false, error: `role insuficiente. Requer: ${roles.join(' | ')}` });
  };
}

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

app.get('/api/users', requireRole('admin'), (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/auth.py users', { timeout: 5000 });
    res.json(JSON.parse(out.toString()));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/users/create', requireRole('admin'), (req, res) => {
  try {
    const { username, password, role } = req.body || {};
    if (!username || !password) return res.status(400).json({ ok: false, error: 'username e password obrigatórios' });
    const r = role ? ` ${JSON.stringify(role)}` : '';
    const out = execSync(`python3 /home/jdl/ai-os/bin/auth.py create ${JSON.stringify(username)} ${JSON.stringify(password)}${r}`, { timeout: 8000 });
    res.json(JSON.parse(out.toString()));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/users/deactivate', requireRole('admin'), (req, res) => {
  try {
    const { username } = req.body || {};
    if (!username) return res.status(400).json({ ok: false, error: 'username obrigatório' });
    const out = execSync(`python3 /home/jdl/ai-os/bin/auth.py deactivate ${JSON.stringify(username)}`, { timeout: 5000 });
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

app.get('/api/agent/status', (req, res) => res.json(nocExec('agent_status')));

// Serve ops.html
app.get('/noc',     (req, res) => res.sendFile(__dirname + '/noc.html'));
app.get('/ops',     (req, res) => res.sendFile(__dirname + '/ops.html'));
app.get('/factory', (req, res) => res.sendFile(__dirname + '/factory.html'));
app.get('/tenders', (req, res) => res.sendFile(__dirname + '/tenders.html'));
app.get('/control', (req, res) => res.sendFile(__dirname + '/control.html'));
app.get('/worker',  (req, res) => res.sendFile(__dirname + '/worker.html'));
app.get('/finance', (req, res) => res.sendFile(__dirname + '/finance.html'));
app.get('/login',   (req, res) => res.sendFile(__dirname + '/login.html'));

// Serve cliente.html (portal cliente público por token) — legado
app.get('/lote/:token',   (req, res) => res.sendFile(__dirname + '/cliente.html'));
// Serve client.html (novo portal cliente G sprint)
app.get('/client/:token', (req, res) => res.sendFile(__dirname + '/client.html'));

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

// Twin Approvals
app.get('/api/twin/approvals', (req, res) => {
  const limit = parseInt(req.query.limit || '20', 10);
  try {
    const DB_URL = process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1:5432/aios';
    const out = execSync(
      `DATABASE_URL=${DB_URL} python3 -c "
import os, json, sqlalchemy as sa
e = sa.create_engine(os.environ['DATABASE_URL'])
with e.connect() as c:
    rows = c.execute(sa.text('SELECT id, action, summary, status, requested_at, decided_at FROM public.twin_approvals ORDER BY requested_at DESC LIMIT :n'), {'n': ${limit}}).mappings().all()
    print(json.dumps([dict(r) for r in rows], default=str))
"`, { timeout: 8000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out.trim()));
  } catch (e) {
    res.status(500).json({ error: String(e.message || e) });
  }
});

// Twin Approvals — set status (requer role supervisor ou superior)
app.post('/api/twin/approvals/:id/set', requireRole('supervisor'), (req, res) => {
  const id     = parseInt(req.params.id, 10);
  const status = String(req.body?.status || '').trim();
  if (!id || !['approved', 'rejected'].includes(status))
    return res.status(400).json({ ok: false, error: 'id e status (approved|rejected) obrigatórios' });
  try {
    const DB_URL = process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1:5432/aios';
    const out = execSync(
      `DATABASE_URL=${DB_URL} python3 -c "
import os, json, sqlalchemy as sa
from datetime import datetime
e = sa.create_engine(os.environ['DATABASE_URL'])
with e.begin() as c:
    r = c.execute(sa.text('UPDATE public.twin_approvals SET status=:s, decided_at=:d WHERE id=:id RETURNING id, action, status, decided_at'), {'s': '${status}', 'd': datetime.utcnow(), 'id': ${id}}).mappings().all()
    if r:
        print(json.dumps({'ok': True, 'approval': dict(r[0])}, default=str))
    else:
        print(json.dumps({'ok': False, 'error': 'approval não encontrada'}))
"`, { timeout: 8000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out.trim()));
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e.message || e) });
  }
});

// Twin Cases (lista)
app.get('/api/twin/cases', (req, res) => {
  const limit = parseInt(req.query.limit || '20', 10);
  res.json(nocExec(`twin_cases ${limit}`));
});

// Twin Batch — portal cliente (público, por token)
app.get('/api/twin/batch/by-token/:token', (req, res) => {
  const token = String(req.params.token || '').replace(/[^a-zA-Z0-9_-]/g, '');
  if (!token) return res.status(400).json({ error: 'token inválido' });
  res.json(nocExec(`twin_batch_by_token ${token}`));
});

// Twin Batch — ver lote
app.get('/api/twin/batch/:id', (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ error: 'id inválido' });
  res.json(nocExec(`twin_batch_get ${id}`));
});

// Twin Batch — avançar estado (OPS/JWT)
app.post('/api/twin/batch/:id/advance', (req, res) => {
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const id   = parseInt(req.params.id, 10);
  const nota = String((req.body || {}).nota || '').replace(/"/g, '').slice(0, 100);
  if (!id) return res.status(400).json({ error: 'id inválido' });
  res.json(nocExec(`twin_batch_advance ${id} "${nota}"`, 10000));
});

// Twin Batch — faturar (cria approval)
app.post('/api/twin/batch/:id/faturar', (req, res) => {
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const id      = parseInt(req.params.id, 10);
  const preco   = parseFloat((req.body || {}).preco_kg);
  if (!id) return res.status(400).json({ error: 'id inválido' });
  if (isNaN(preco)) return res.status(400).json({ ok: false, error: 'preco_kg obrigatório' });
  res.json(nocExec(`twin_batch_faturar ${id} ${preco}`, 10000));
});

// Twin Batch — faturar_ok (chamado após approval aprovada)
app.post('/api/twin/batch/:id/faturar_ok', (req, res) => {
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ error: 'id inválido' });
  res.json(nocExec(`twin_batch_faturar_ok ${id}`, 10000));
});

// Twin Factory stats — requer role factory/supervisor/admin
app.get('/api/twin/factory/stats', requireRole('factory'), (req, res) => {
  res.json(nocExec('twin_factory_stats'));
});

// Twin Tenders (radar nacional) — requer role viewer ou superior
app.get('/api/twin/tenders', requireRole('viewer'), (req, res) => {
  const limit  = parseInt(req.query.limit, 10) || 100;
  const source = String(req.query.source || '').replace(/[^a-z]/g, '');
  const srcArg = source ? ` --source ${source}` : '';
  res.json(nocExec(`twin_tenders ${limit}${srcArg}`));
});

// Twin Tender — actualizar estado (requer role operator ou superior)
app.post('/api/twin/tender/:id/estado', requireRole('operator'), (req, res) => {
  const id     = parseInt(req.params.id, 10);
  const estado = String(req.body?.estado || '').trim();
  if (!id || !estado) return res.status(400).json({ ok: false, error: 'id e estado obrigatórios' });
  res.json(nocExec(`twin_tender_update ${id} ${estado}`, 8000));
});

// ── Document Vault ───────────────────────────────────────────────────────────
app.get('/api/docs/summary', requireRole('viewer'), (req, res) => {
  res.json(nocExec('doc_summary'));
});

app.get('/api/docs', requireRole('viewer'), (req, res) => {
  const limit  = parseInt(req.query.limit, 10) || 30;
  const status = String(req.query.status || '').replace(/[^a-z_]/g, '');
  const otype  = String(req.query.owner_type || '').replace(/[^a-z_]/g, '');
  let cmd = `doc_list ${limit}`;
  if (status) cmd += ` --status ${status}`;
  if (otype)  cmd += ` --owner-type ${otype}`;
  res.json(nocExec(cmd));
});

app.get('/api/docs/expiring', requireRole('viewer'), (req, res) => {
  const days = parseInt(req.query.days, 10) || 30;
  res.json(nocExec(`doc_expiring ${days}`));
});

app.get('/api/docs/requests', requireRole('viewer'), (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`doc_requests ${limit}`));
});

// ── Vehicles ─────────────────────────────────────────────────────────────────
app.get('/api/vehicles', requireRole('viewer'), (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`vehicle_list ${limit}`));
});

app.get('/api/vehicles/:id', requireRole('viewer'), (req, res) => {
  const id = String(req.params.id).replace(/[^a-zA-Z0-9-]/g, '');
  res.json(nocExec(`vehicle_get ${id}`));
});

app.get('/api/companies', requireRole('viewer'), (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`company_list ${limit}`));
});

app.get('/api/persons', requireRole('viewer'), (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`person_list ${limit}`));
});

// Twin Batch — guia pública via client_token (portal cliente)
// Rota: GET /api/twin/batch/by-token/:token/doc/guia
app.get('/api/twin/batch/by-token/:token/doc/guia', (req, res) => {
  const token = String(req.params.token || '').replace(/[^a-zA-Z0-9_-]/g, '');
  if (!token) return res.status(400).json({ error: 'token inválido' });
  // Descobrir entity_id pelo token
  const lookup = nocExec(`twin_batch_by_token ${token}`);
  if (lookup.error || !lookup.entity_id)
    return res.status(404).json({ error: 'lote não encontrado' });
  const id = parseInt(lookup.entity_id, 10);
  try {
    const out = execSync(`python3 /home/jdl/ai-os/bin/doc_engine.py guia ${id}`, { timeout: 15000 });
    const result = JSON.parse(out.toString());
    if (!result.ok) return res.status(500).json(result);
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `inline; filename="${result.filename}"`);
    res.send(fs.readFileSync(result.path));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Twin Batch — gerar doc PDF (guia | fatura) — requer OPS token ou JWT
app.get('/api/twin/batch/:id/doc/:type', (req, res) => {
  // Nota: /twin/batch bypassa o middleware global, por isso verificamos JWT manualmente
  const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
  const opsOk  = OPS_TOKEN && opsTok === OPS_TOKEN;
  const auth   = req.headers['authorization'] || '';
  const tok    = auth.startsWith('Bearer ') ? auth.slice(7) : null;
  const jwtOk  = tok ? !!verifyJWT(tok) : false;
  if (!opsOk && !jwtOk)
    return res.status(401).json({ ok: false, error: 'autenticação necessária para aceder a documentos' });

  const id   = parseInt(req.params.id, 10);
  const type = req.params.type;
  if (!id || !['guia','fatura'].includes(type))
    return res.status(400).json({ error: 'id ou tipo inválido' });
  try {
    const out = execSync(`python3 /home/jdl/ai-os/bin/doc_engine.py ${type} ${id}`, { timeout: 15000 });
    const result = JSON.parse(out.toString());
    if (!result.ok) return res.status(422).json(result);
    res.setHeader('Content-Type', 'application/pdf');
    res.setHeader('Content-Disposition', `attachment; filename="${result.filename}"`);
    res.send(fs.readFileSync(result.path));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Twin Batch — registar resultado (OPS/JWT)
app.post('/api/twin/batch/:id/resultado', (req, res) => {
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const id  = parseInt(req.params.id, 10);
  const { kg_cobre, kg_plastico } = req.body || {};
  if (!id) return res.status(400).json({ error: 'id inválido' });
  if (kg_cobre == null || kg_plastico == null)
    return res.status(400).json({ ok: false, error: 'kg_cobre e kg_plastico obrigatórios' });
  res.json(nocExec(`twin_batch_resultado ${id} ${parseFloat(kg_cobre)} ${parseFloat(kg_plastico)}`, 10000));
});

// Twin Cable Batch (criar lote — OPS/JWT)
app.post('/api/twin/cable_batch', (req, res) => {
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const { kg, client } = req.body || {};
  if (!kg || !client) return res.status(400).json({ ok: false, error: 'kg e client obrigatórios' });
  const result = nocExec(`twin_cable_batch_create ${parseFloat(kg)} "${String(client).replace(/"/g,'')}"`, 10000);
  if (result.error) return res.status(500).json({ ok: false, error: result.error });
  res.json(result);
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
  // Aceita JWT (via middleware) OU X-AIOS-OPS-TOKEN
  if (!req.user) {
    const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
    if (!OPS_TOKEN || opsTok !== OPS_TOKEN)
      return res.status(401).json({ ok: false, error: 'ops_token_required' });
  }
  const { kind, payload, target_worker_id } = req.body || {};
  if (!kind) return res.status(400).json({ ok: false, error: 'kind required' });
  const payloadJson = JSON.stringify(payload || {}).replace(/'/g, '"');
  const target      = target_worker_id || '-';
  const result      = nocExec(`worker_jobs_enqueue ${kind} '${payloadJson}' ${target}`);
  if (result.error) return res.status(500).json({ ok: false, error: result.error });
  res.json(result);
});

// Acções de controlo — requerem JWT ou OPS token
function requireActionAuth(req, res, next) {
  if (req.user) return next();  // JWT já validado pelo middleware global
  const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
  if (OPS_TOKEN && opsTok === OPS_TOKEN) return next();
  return res.status(401).json({ ok: false, error: 'auth_required' });
}

app.post('/api/actions/tick', requireActionAuth, (req, res) => {
  exec(`sudo systemctl start aios-autopilot.service 2>&1 || python3 ${AIOS_ROOT}/bin/autopilot_tick.py`, (err, out) => {
    res.json({ ok: !err, output: (out || '').slice(0, 200) });
  });
});

app.post('/api/actions/watchdog', requireActionAuth, (req, res) => {
  exec(`sudo systemctl start aios-watchdog.service 2>&1`, (err, out) => {
    res.json({ ok: !err, output: (out || '').slice(0, 200) });
  });
});

app.post('/api/actions/healthcheck', requireActionAuth, (req, res) => {
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

// ── client portal (token-based, public) ───────────────────────────────────────

// POST /api/twin/client/:token/approve/:id  — cliente aprova/rejeita via portal
app.post('/api/twin/client/:token/approve/:id', (req, res) => {
  const token  = String(req.params.token || '').replace(/[^a-zA-Z0-9_-]/g, '');
  const id     = parseInt(req.params.id, 10);
  const action = String(req.body?.action || '').trim();
  if (!token || !id || !['approved', 'rejected'].includes(action))
    return res.status(400).json({ ok: false, error: 'token, id e action (approved|rejected) obrigatórios' });
  res.json(nocExec(`client_approve ${token} ${id} ${action}`, 10000));
});

// ── worker app (twin tasks) ────────────────────────────────────────────────────
const WORKER_ROLES = ['worker','operator','factory','supervisor','admin'];

app.get('/api/worker/tasks', requireRole(...WORKER_ROLES), (req, res) => {
  const user  = req.user.username || 'all';
  const limit = parseInt(req.query.limit, 10) || 30;
  res.json(nocExec(`worker_tasks ${user} ${limit}`));
});

app.get('/api/worker/tasks/history', requireRole(...WORKER_ROLES), (req, res) => {
  const user  = req.user.username || 'all';
  const limit = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`worker_tasks_history ${user} ${limit}`));
});

app.post('/api/worker/tasks/:id/start', requireRole(...WORKER_ROLES), (req, res) => {
  const id   = parseInt(req.params.id, 10);
  const user = req.user.username;
  if (!id || !user) return res.status(400).json({ ok: false, error: 'id e username obrigatórios' });
  res.json(nocExec(`worker_task_start ${id} ${user}`));
});

app.post('/api/worker/tasks/:id/done', requireRole(...WORKER_ROLES), (req, res) => {
  const id   = parseInt(req.params.id, 10);
  const user = req.user.username;
  const note = String(req.body?.note || '').replace(/'/g, '"').slice(0, 500);
  if (!id || !user) return res.status(400).json({ ok: false, error: 'id e username obrigatórios' });
  res.json(nocExec(`worker_task_done ${id} ${user} '${note}'`));
});

app.post('/api/worker/tasks/:id/skip', requireRole(...WORKER_ROLES), (req, res) => {
  const id   = parseInt(req.params.id, 10);
  const user = req.user.username;
  if (!id || !user) return res.status(400).json({ ok: false, error: 'id e username obrigatórios' });
  res.json(nocExec(`worker_task_skip ${id} ${user}`));
});

// ── finance twin ──────────────────────────────────────────────────────────────

const FINANCE_ROLES = ['finance', 'supervisor', 'admin', 'factory'];

app.get('/api/twin/invoices', requireRole(...FINANCE_ROLES), (req, res) => {
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit, 10) || 20;
  const args   = [status, String(limit)].filter(Boolean).join(' ');
  res.json(nocExec(`finance_invoice_list ${args}`));
});

app.post('/api/twin/invoices/:id/pay', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  const paid_at = (req.body || {}).paid_at || '';
  res.json(nocExec(`finance_invoice_pay ${id}${paid_at ? ' ' + paid_at : ''}`));
});

app.get('/api/twin/finance/stats', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('finance_stats'));
});

// ── timesheets ─────────────────────────────────────────────────────────────────

app.get('/api/worker/timesheets', requireRole(...WORKER_ROLES), (req, res) => {
  const user   = req.user.username;
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit, 10) || 30;
  res.json(nocExec(`timesheet_list ${user} ${status} ${limit}`));
});

app.post('/api/worker/timesheets/start', requireRole(...WORKER_ROLES), (req, res) => {
  const user       = req.user.username;
  const eventName  = String(req.body?.event_name || '').replace(/'/g, '"').slice(0, 200);
  const hourlyRate = parseFloat(req.body?.hourly_rate) || '';
  if (!eventName) return res.status(400).json({ ok: false, error: 'event_name obrigatório' });
  const args = hourlyRate ? `${user} '${eventName}' ${hourlyRate}` : `${user} '${eventName}'`;
  res.json(nocExec(`timesheet_start ${args}`));
});

app.post('/api/worker/timesheets/:id/stop', requireRole(...WORKER_ROLES), (req, res) => {
  const id    = parseInt(req.params.id, 10);
  const notes = String(req.body?.notes || '').replace(/'/g, '"').slice(0, 500);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  const args = notes ? `${id} '${notes}'` : String(id);
  res.json(nocExec(`timesheet_stop ${args}`));
});

app.get('/api/timesheets/all', requireRole(...FINANCE_ROLES), (req, res) => {
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit, 10) || 50;
  res.json(nocExec(`timesheet_all ${status} ${limit}`));
});

app.post('/api/timesheets/:id/approve', requireRole('supervisor', 'admin', 'finance'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`timesheet_approve ${id}`));
});

// ── finance obligations ────────────────────────────────────────────────────────

app.get('/api/twin/obligations', requireRole(...FINANCE_ROLES), (req, res) => {
  const status = req.query.status || '';
  const days   = parseInt(req.query.days, 10) || 90;
  const limit  = parseInt(req.query.limit, 10) || 50;
  res.json(nocExec(`obligation_list ${status} ${days} ${limit}`));
});

app.post('/api/twin/obligations/:id/pay', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`obligation_pay ${id}`));
});

app.post('/api/twin/obligations/:id/approve', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`obligation_approve ${id}`));
});

app.get('/api/twin/obligations/stats', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('obligation_stats'));
});

// ── people + clients ───────────────────────────────────────────────────────────

app.get('/api/people', requireRole(...FINANCE_ROLES), (req, res) => {
  const cluster = req.query.cluster || '';
  res.json(nocExec(`people_list ${cluster}`));
});

app.get('/api/clients', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('client_list'));
});

// ── client portal — timesheets ─────────────────────────────────────────────────
// Público (por token) — AUTH_EXEMPT via path check abaixo

app.get('/api/client/:token/timesheets', (req, res) => {
  const token  = String(req.params.token || '').replace(/[^a-zA-Z0-9]/g, '');
  const status = String(req.query.status || '').replace(/[^a-z]/g, '');
  const limit  = parseInt(req.query.limit, 10) || 50;
  if (!token) return res.status(400).json({ ok: false, error: 'token obrigatório' });
  res.json(nocExec(`client_timesheets ${token} ${status} ${limit}`, 8000));
});

app.post('/api/client/:token/timesheet/:id/approve', (req, res) => {
  const token  = String(req.params.token || '').replace(/[^a-zA-Z0-9]/g, '');
  const id     = parseInt(req.params.id, 10);
  if (!token || !id) return res.status(400).json({ ok: false, error: 'token e id obrigatórios' });
  res.json(nocExec(`client_timesheet_action ${token} ${id} approved`, 8000));
});

app.post('/api/client/:token/timesheet/:id/reject', (req, res) => {
  const token  = String(req.params.token || '').replace(/[^a-zA-Z0-9]/g, '');
  const id     = parseInt(req.params.id, 10);
  if (!token || !id) return res.status(400).json({ ok: false, error: 'token e id obrigatórios' });
  res.json(nocExec(`client_timesheet_action ${token} ${id} rejected`, 8000));
});

// ── payouts ────────────────────────────────────────────────────────────────────

app.get('/api/finance/payouts', requireRole(...FINANCE_ROLES), (req, res) => {
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit, 10) || 50;
  res.json(nocExec(`payout_list ${status} ${limit}`));
});

app.post('/api/finance/payouts/run', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const week = String(req.body?.week_start || '').replace(/[^0-9-]/g, '');
  if (!week) return res.status(400).json({ ok: false, error: 'week_start obrigatório (YYYY-MM-DD)' });
  res.json(nocExec(`payout_run ${week}`, 15000));
});

app.post('/api/finance/payouts/:id/paid', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`payout_mark_paid ${id}`));
});

// ── invoice engine (H2) ───────────────────────────────────────────────────────

app.post('/api/finance/invoice/generate', requireRole(...FINANCE_ROLES), (req, res) => {
  const event  = String(req.body?.event_name || '').trim();
  const client = parseInt(req.body?.client_id, 10) || '';
  if (!event) return res.status(400).json({ ok: false, error: 'event_name obrigatório' });
  res.json(nocExec(`invoice_generate ${JSON.stringify(event)}${client ? ' ' + client : ''}`, 10000));
});

app.post('/api/finance/invoice/:id/push_toconline', requireRole('finance', 'supervisor', 'admin'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`invoice_push_toc ${id}`, 20000));
});

app.get('/api/finance/invoice/drafts', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('invoice_drafts'));
});

app.get('/api/finance/toconline/health', (req, res) => {
  // public health check — só diz se está ligado, sem dados sensíveis
  const r = nocExec('toconline_status');
  const drafts = nocExec('invoice_drafts');
  const pending = (drafts.drafts || []).filter(d => !d.toconline_id).length;
  const last_push = (drafts.drafts || []).filter(d => d.toconline_id)
    .sort((a, b) => b.created_at > a.created_at ? 1 : -1)[0];
  res.json({
    ok: r.ok,
    status: r.toconline || 'error',
    connected: r.ok && r.toconline === 'connected',
    drafts_pending_push: pending,
    last_push_at: last_push ? last_push.created_at : null,
    hint: r.ok ? null : (r.hint || 'Token expirado — actualizar .toc_token.json'),
  });
});

app.get('/api/finance/toconline/status', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('toconline_status', 10000));
});

// ── Reconciliação bancária ─────────────────────────────────────────────────────

app.get('/api/finance/bank/transactions', requireRole(...FINANCE_ROLES), (req, res) => {
  const limit  = parseInt(req.query.limit, 10) || 100;
  const status = req.query.status || '';
  const args   = status ? `${limit} ${status}` : `${limit}`;
  res.json(nocExec(`bank_transactions ${args}`));
});

app.post('/api/finance/bank/reconcile', requireRole(...FINANCE_ROLES), (req, res) => {
  res.json(nocExec('bank_reconcile', 30000));
});

app.post('/api/finance/bank/match', requireRole(...FINANCE_ROLES), (req, res) => {
  const tx_id  = parseInt(req.body?.transaction_id, 10);
  const inv_id = parseInt(req.body?.invoice_id, 10);
  if (!tx_id || !inv_id) return res.status(400).json({ ok: false, error: 'transaction_id e invoice_id obrigatórios' });
  res.json(nocExec(`bank_match ${tx_id} ${inv_id}`));
});

app.post('/api/finance/bank/ignore', requireRole(...FINANCE_ROLES), (req, res) => {
  const tx_id = parseInt(req.body?.transaction_id, 10);
  if (!tx_id) return res.status(400).json({ ok: false, error: 'transaction_id obrigatório' });
  res.json(nocExec(`bank_ignore ${tx_id}`));
});

// Upload CSV — usa multipart com busboy para não precisar npm extra
app.post('/api/finance/bank/import', requireRole(...FINANCE_ROLES), (req, res) => {
  const os   = require('os');
  const path = require('path');
  const { execFileSync } = require('child_process');
  const tmpFile = path.join(os.tmpdir(), `bank_import_${Date.now()}.csv`);
  const chunks  = [];

  req.on('data', d => chunks.push(d));
  req.on('end', () => {
    try {
      const body = Buffer.concat(chunks);
      // Tentar extrair conteúdo CSV do multipart ou body directo
      let csvContent = '';
      const contentType = req.headers['content-type'] || '';
      if (contentType.includes('multipart/form-data')) {
        // Extrai a parte CSV (heurística: primeiro bloco após boundary)
        const bodyStr = body.toString('utf-8');
        const parts   = bodyStr.split(/--[a-zA-Z0-9]+\r?\n/);
        for (const part of parts) {
          if (part.includes('filename=') || part.includes('name=')) {
            const dataStart = part.indexOf('\r\n\r\n');
            if (dataStart >= 0) {
              csvContent = part.slice(dataStart + 4).replace(/\r?\n--[^\r\n]+.*$/s, '').trim();
              break;
            }
          }
        }
      } else {
        // body directo (text/csv ou application/octet-stream)
        csvContent = body.toString('utf-8');
      }

      if (!csvContent) {
        return res.status(400).json({ ok: false, error: 'CSV vazio ou formato não reconhecido' });
      }

      fs.writeFileSync(tmpFile, csvContent, 'utf-8');
      const out = execFileSync('python3', ['bin/reconcile.py', 'bank_import', tmpFile], {
        cwd: process.env.HOME + '/ai-os',
        timeout: 15000,
        env: { ...process.env }
      }).toString();
      try { fs.unlinkSync(tmpFile); } catch(_) {}
      res.json(JSON.parse(out));
    } catch(e) {
      try { fs.unlinkSync(tmpFile); } catch(_) {}
      res.status(500).json({ ok: false, error: String(e) });
    }
  });
});

app.post('/api/worker/timesheets/:id/submit', requireRole(...WORKER_ROLES), (req, res) => {
  const id    = parseInt(req.params.id, 10);
  const notes = String(req.body?.notes || '').trim();
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`timesheet_submit ${id}${notes ? ' ' + JSON.stringify(notes) : ''}`));
});

// ── Painel do João — ideias ────────────────────────────────────────────────────
const JOAO_ROLES = ['admin', 'supervisor'];

app.get('/joao', (req, res) => res.sendFile(__dirname + '/joao.html'));

app.get('/api/ideas', requireRole(...JOAO_ROLES), (req, res) => {
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit, 10) || 30;
  res.json(nocExec(`idea_list ${status} ${limit}`));
});

app.post('/api/ideas', requireRole(...JOAO_ROLES), (req, res) => {
  const title   = String(req.body?.title   || '').trim();
  const message = String(req.body?.message || '').trim();
  if (!title) return res.status(400).json({ ok: false, error: 'title obrigatório' });
  const args = [JSON.stringify(title), ...(message ? [JSON.stringify(message)] : [])];
  res.json(nocExec(`idea_create ${args.join(' ')}`));
});

app.get('/api/ideas/:id', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`idea_get ${id}`));
});

app.post('/api/ideas/:id/analyze', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  // Enfileira no cluster via worker_jobs pipeline
  const result = nocExec(`pipeline_idea_analyze ${id}`, 5000);
  if (result && result.ok) {
    res.json({ ok: true, id, job_id: result.job_id, status: 'analyzing', message: 'Análise enfileirada no cluster...' });
  } else {
    res.json(result || { ok: false, error: 'Erro ao enfileirar análise' });
  }
});

app.get('/api/ideas/:id/reviews', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`idea_reviews ${id}`));
});

app.post('/api/ideas/:id/create_case', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`idea_create_case ${id}`, 10000));
});

app.post('/api/ideas/:id/archive', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`idea_archive ${id}`));
});

// ── Decisões ───────────────────────────────────────────────────────────────────

app.get('/api/decisions', requireRole(...JOAO_ROLES), (req, res) => {
  const status = req.query.status || 'pending';
  const limit  = parseInt(req.query.limit, 10) || 20;
  res.json(nocExec(`decision_list ${status} ${limit}`));
});

app.post('/api/decisions', requireRole(...JOAO_ROLES), (req, res) => {
  const { title, kind } = req.body || {};
  if (!title) return res.status(400).json({ ok: false, error: 'title obrigatório' });
  const safeTitle = String(title).replace(/'/g, "\\'").slice(0, 200);
  const safeKind  = ['manual','finance_task','decision_task','project_task','ops_task'].includes(kind) ? kind : 'manual';
  res.json(nocExec(`decision_create '${safeTitle}' ${safeKind}`));
});

app.post('/api/decisions/:id/resolve', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`decision_resolve ${id}`));
});

// ── Agent suggestions (Chief of Staff) ────────────────────────────────────────

app.get('/api/agent/suggestions', requireRole(...JOAO_ROLES), (req, res) => {
  const kind  = req.query.kind  || '';
  const limit = parseInt(req.query.limit, 10) || 30;
  res.json(nocExec(`agent_suggestions ${kind || ''} ${limit}`));
});

app.get('/api/agent/briefing', requireRole(...JOAO_ROLES), (req, res) => {
  res.json(nocExec('agent_briefing'));
});

app.post('/api/agent/suggestions/:id/read', requireRole(...JOAO_ROLES), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`agent_suggestion_read ${id}`));
});

// Trigger a manual agent cycle (async, does not wait)
app.post('/api/agent/run', requireRole(...JOAO_ROLES), (req, res) => {
  const mode = ['morning','midday','evening'].includes(req.body?.mode) ? req.body.mode : 'morning';
  const { spawn } = require('child_process');
  const proc = spawn('python3', ['bin/joao_agent.py', mode], {
    cwd: process.env.HOME + '/ai-os',
    detached: true,
    stdio: 'ignore',
    env: { ...process.env }
  });
  proc.unref();
  res.json({ ok: true, mode, note: 'cycle started in background' });
});

// ── Incidents ──────────────────────────────────────────────────────────────────

app.get('/api/incidents', requireRole('admin','supervisor','operator'), (req, res) => {
  const limit = parseInt(req.query.limit, 10) || 50;
  res.json(nocExec(`incident_list ${limit}`));
});

app.post('/api/incidents/:id/resolve', requireRole('admin','supervisor'), (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
  res.json(nocExec(`incident_resolve ${id}`));
});

// ── Control Room — agregador ────────────────────────────────────────────────────

app.get('/api/control/overview', (req, res) => {
  try {
    // Parallel reads via nocExec (synchronous but fast — cached Postgres)
    const tenders    = nocExec('twin_tenders 20 --pin-sources');
    const workers    = nocExec('workers');
    const tasks      = nocExec('backlog_recent 20');
    const obls       = nocExec('obligation_list 30');
    const payouts    = nocExec('payout_list 20');
    const alerts     = nocExec('agent_suggestions alert 10');
    const health     = nocExec('syshealth');
    const finStats   = nocExec('finance_stats');
    const cases      = nocExec('twin_cases 8');
    const ideas      = nocExec('idea_list _ 8');
    const incidents  = nocExec('incident_list 20');
    const bankTxs    = nocExec('bank_transactions 5 unmatched');
    const clusterJobs    = nocExec('worker_jobs 15');
    const clusterMetrics = nocExec('cluster_metrics 6');
    const agentStatus    = nocExec('agent_status');
    const docSummary     = nocExec('doc_summary');

    const arr = v => Array.isArray(v) ? v : [];
    res.json({
      ok: true,
      tenders:       tenders.tenders   || arr(tenders),
      workers:       workers.workers   || arr(workers),
      tasks:         tasks.tasks       || arr(tasks),
      obligations:   obls.obligations  || [],
      payouts:       payouts.payouts   || [],
      alerts:        alerts.suggestions || [],
      health:        health,
      finance_stats: finStats,
      cases:         cases.cases       || arr(cases),
      ideas:         ideas.ideas       || arr(ideas),
      incidents:     incidents.incidents || [],
      bank_unmatched: bankTxs.summary?.unmatched || 0,
      cluster_jobs:    arr(clusterJobs),
      cluster_metrics: arr(clusterMetrics),
      agent_status:    arr(agentStatus),
      doc_summary:     docSummary,
      generated_at:  new Date().toISOString(),
    });
  } catch(e) {
    res.json({ ok: false, error: String(e) });
  }
});

// ── versão e boot ─────────────────────────────────────────────────────────────

app.get('/api/version', (req, res) => res.json({ version: '1.0.0', name: 'ai-os' }));

// ── Model Router ───────────────────────────────────────────────────────────────

// Estado do routing (público — sem dados sensíveis)
app.get('/api/model-router/state', (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/provider_health.py', { timeout: 10000, encoding: 'utf8' });
    const health = JSON.parse(out);

    // Lê override de runtime
    let override = '';
    try {
      const oFile = AIOS_ROOT + '/runtime/model_override.json';
      if (fs.existsSync(oFile)) {
        override = JSON.parse(fs.readFileSync(oFile, 'utf8')).force_model || '';
      }
    } catch(e) {}

    // Determina estado visual
    const forceEnv  = (process.env.FORCE_MODEL || '').toLowerCase().trim();
    const force     = forceEnv || override;
    const claudeOk  = health.claude?.available;
    const ollamaOk  = health.ollama?.available;

    let active_provider = 'unknown';
    let routing_mode    = 'auto';
    if (force === 'claude')  { active_provider = 'claude'; routing_mode = 'forced_claude'; }
    else if (force === 'ollama') { active_provider = 'ollama'; routing_mode = 'forced_ollama'; }
    else if (claudeOk)       { active_provider = 'claude'; routing_mode = 'auto'; }
    else if (ollamaOk)       { active_provider = 'ollama'; routing_mode = 'fallback'; }
    else                     { active_provider = 'none';   routing_mode = 'degraded'; }

    res.json({
      ok: true,
      active_provider,
      routing_mode,
      force_model:    force || null,
      hybrid_mode:    (process.env.HYBRID_MODE || 'true') === 'true',
      claude:         health.claude,
      ollama:         health.ollama,
      last_fallback:  health.last_fallback,
      last_routing:   health.last_routing,
      ts:             health.ts,
    });
  } catch(e) {
    res.json({ ok: false, error: String(e) });
  }
});

// ── Council (AI Council) ──────────────────────────────────────────────────────
app.get('/api/council', requireRole('viewer'), (req, res) => {
  try {
    const kind = req.query.kind ? ` ${req.query.kind}` : '';
    const out  = execSync(`python3 /home/jdl/ai-os/bin/council.py list${kind} --limit 10`, { timeout: 15000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/council/analyze', requireRole('operator'), (req, res) => {
  try {
    const { topic, kind } = req.body || {};
    if (!topic) return res.status(400).json({ ok: false, error: 'topic required' });
    const safeTopic = String(topic).replace(/'/g, "'\\''").slice(0, 500);
    const safeKind  = ['idea','decision','project','architecture','problem','general'].includes(kind) ? kind : 'general';
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/council.py analyze '${safeTopic}' --kind ${safeKind}`,
      { timeout: 120000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// ── Knowledge (Qdrant) ────────────────────────────────────────────────────────
app.get('/api/knowledge/stats', requireRole('viewer'), (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/knowledge.py stats', { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/knowledge', requireRole('viewer'), (req, res) => {
  try {
    const kind = req.query.kind ? ` ${req.query.kind}` : '';
    const out  = execSync(`python3 /home/jdl/ai-os/bin/knowledge.py list${kind}`, { timeout: 15000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/knowledge/search', requireRole('viewer'), (req, res) => {
  try {
    const q    = String(req.query.q || '').replace(/'/g, "'\\''");
    const kind = req.query.kind ? ` --kind ${req.query.kind}` : '';
    const out  = execSync(`python3 /home/jdl/ai-os/bin/knowledge.py search '${q}'${kind}`, { timeout: 20000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/knowledge', requireRole('operator'), (req, res) => {
  try {
    const { kind, text, meta } = req.body || {};
    if (!kind || !text) return res.status(400).json({ ok: false, error: 'kind and text required' });
    const safeText = String(text).replace(/'/g, "'\\''");
    const safeMeta = JSON.stringify(meta || {}).replace(/'/g, "'\\''");
    const out = execSync(`python3 /home/jdl/ai-os/bin/knowledge.py add ${kind} '${safeText}' --meta '${safeMeta}'`,
                         { timeout: 20000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.delete('/api/knowledge/:id', requireRole('operator'), (req, res) => {
  try {
    const out = execSync(`python3 /home/jdl/ai-os/bin/knowledge.py delete ${req.params.id}`,
                         { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// Forçar modelo (requer admin)
app.post('/api/model-router/force', requireRole('admin'), (req, res) => {
  const { force_model } = req.body || {};
  const allowed = ['claude', 'ollama', ''];
  const val = String(force_model || '').toLowerCase().trim();
  if (!allowed.includes(val))
    return res.status(400).json({ ok: false, error: 'force_model deve ser "claude", "ollama" ou "" (limpar)' });
  try {
    const arg = JSON.stringify(val);
    const out = execSync(`python3 /home/jdl/ai-os/bin/model_router.py set_override ${arg}`, { timeout: 5000, encoding: 'utf8' });
    res.json({ ...JSON.parse(out), force_model: val || null });
  } catch(e) {
    res.json({ ok: false, error: String(e) });
  }
});

// Créditos/uso (requer admin)
app.get('/api/model-router/credits', requireRole('admin'), (req, res) => {
  try {
    const out = execSync('python3 /home/jdl/ai-os/bin/credit_monitor.py', { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) {
    res.json({ ok: false, error: String(e) });
  }
});

// Adiciona model-router/state à whitelist pública (dashboard não requer JWT)
AUTH_EXEMPT.add('/model-router/state');

app.listen(3000, () => console.log("UI http://localhost:3000"));
