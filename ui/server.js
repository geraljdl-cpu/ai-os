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
    // Normalise client_id
    if (payload.client_id !== undefined && payload.client_id !== null)
      payload.client_id = parseInt(payload.client_id, 10);
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
  if (rec.count > 500) {
    return res.status(429).json({ ok: false, error: 'rate limit exceeded (500 req/min)' });
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
  // WhatsApp ponto webhook (Twilio envia sem JWT)
  '/whatsapp/inbound', '/whatsapp/status', '/whatsapp/health',
]);
app.use('/api', (req, res, next) => {
  if (AUTH_EXEMPT.has(req.path)) return next();
  if (req.path.startsWith('/twin/batch'))  return next();  // auth própria em cada endpoint
  if (req.path.startsWith('/twin/client')) return next(); // client portal — token validado no noc_query
  if (req.path.match(/^\/client\/[a-zA-Z0-9]+\/(timesheets|timesheet)/)) return next(); // client timesheet portal
  if (req.path.match(/^\/service\/validate\/[a-zA-Z0-9\-]+/)) return next(); // service validation (public token)
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
// RBAC: admin > supervisor > operator/factory > finance > viewer > show > cliente/client_*
const ROLE_LEVEL = {
  admin:100, supervisor:80, operator:60, factory:60, finance:50, viewer:30,
  worker:25, show:20, cliente:10,
  client_manager:45, client_accounting:40,
};
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

// --- requireClientAccess: role ≥ client_accounting AND client_id set (or admin) ---
function requireClientAccess(req, res, next) {
  const opsTok = String(req.headers['x-aios-ops-token'] || '').trim();
  if (OPS_TOKEN && opsTok === OPS_TOKEN) return next();
  if (!req.user) return res.status(401).json({ ok: false, error: 'unauthorized' });
  const lvl = ROLE_LEVEL[req.user.role] || 0;
  if (lvl >= 80) return next(); // admin/supervisor always pass
  if (lvl < ROLE_LEVEL['client_accounting'])
    return res.status(403).json({ ok: false, error: 'acesso negado' });
  if (!req.user.client_id)
    return res.status(403).json({ ok: false, error: 'utilizador sem cliente associado' });
  next();
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
    const { username, password, role, client_id } = req.body || {};
    if (!username || !password) return res.status(400).json({ ok: false, error: 'username e password obrigatórios' });
    const r   = role      ? ` ${JSON.stringify(role)}` : '';
    const cid = client_id ? ` --client-id ${parseInt(client_id,10)}` : '';
    const out = execSync(`python3 /home/jdl/ai-os/bin/auth.py create ${JSON.stringify(username)} ${JSON.stringify(password)}${r}${cid}`, { timeout: 8000 });
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
app.get('/seguros', (req, res) => res.sendFile(__dirname + '/joao.html'));
app.get('/quick',   (req, res) => res.sendFile(__dirname + '/quick.html'));
app.get('/capture', (req, res) => res.sendFile(__dirname + '/quick.html'));
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
  try {
    const { Client } = require('pg');
    const client = new Client({ host:'127.0.0.1', database:'aios', user:'aios_user', password:'jdl' });
    client.connect();
    client.query(`
      SELECT id, matricula, marca, modelo, ano, cor, estado, owner_type,
             combustivel, km_atual, notes,
             inspecao_data, inspecao_proxima, iuc_data, iuc_valor,
             seguro_apolice, seguro_validade,
             (inspecao_proxima - CURRENT_DATE)::int AS dias_inspecao,
             (iuc_data         - CURRENT_DATE)::int AS dias_iuc,
             (seguro_validade  - CURRENT_DATE)::int AS dias_seguro
      FROM public.vehicles WHERE estado != 'inativo' ORDER BY matricula
    `, (err, r) => {
      client.end();
      if (err) return res.status(500).json({ ok: false, error: err.message });
      res.json({ ok: true, vehicles: r.rows });
    });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e) }); }
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
  const limit  = parseInt(req.query.limit || '30', 10);
  const status = String(req.query.status || '').replace(/[^a-z_]/g, '');
  res.json(nocExec(status ? `worker_jobs ${limit} ${status}` : `worker_jobs ${limit}`));
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
  const result = nocExec(`timesheet_approve ${id}`);
  if (result.ok) {
    // Fire-and-forget: criar rascunho mock sem bloquear resposta
    require('child_process').exec(
      `python3 /home/jdl/ai-os/bin/invoice_mock.py draft_ts ${id}`,
      (err, stdout) => { if (err) console.error('[mock draft]', err.message?.slice(0,100)); }
    );
  }
  res.json(result);
});

// Manual entry + promote
app.post('/api/service/manual', requireRole('supervisor', 'admin', 'finance'), (req, res) => {
  try {
    const { person_id, log_date, start_time, end_time, event_name, notes, car_used, client_id } = req.body || {};
    if (!person_id || !log_date || !start_time || !end_time)
      return res.status(400).json({ ok: false, error: 'person_id, log_date, start_time, end_time obrigatorios' });
    const args = [
      String(parseInt(person_id, 10)), String(log_date),
      String(start_time), String(end_time),
    ];
    if (event_name) args.push('--event', String(event_name).slice(0, 100));
    if (notes)      args.push('--notes', String(notes).slice(0, 300));
    if (car_used)   args.push('--car');
    if (client_id)  args.push('--client-id', String(parseInt(client_id, 10)));
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['/home/jdl/ai-os/bin/service_billing.py', 'manual', ...args],
      { timeout: 10000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 300) }); }
});

app.post('/api/timesheets/:id/promote', requireRole('supervisor', 'admin', 'finance'), (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    if (!id) return res.status(400).json({ ok: false, error: 'id invalido' });
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['/home/jdl/ai-os/bin/service_billing.py', 'promote', String(id)],
      { timeout: 10000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 300) }); }
});

app.get('/api/service/persons', requireRole('viewer'), (req, res) => {
  try {
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['-c',
      `import psycopg2,json; conn=psycopg2.connect("dbname=aios user=aios_user password=jdl host=127.0.0.1"); cur=conn.cursor(); cur.execute("SELECT id,name FROM public.persons WHERE status IS DISTINCT FROM 'inactive' ORDER BY name"); print(json.dumps([{"id":r[0],"name":r[1]} for r in cur.fetchall()]))`
    ], { timeout: 5000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e) }); }
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
  const source  = String(req.body?.source  || 'manual').trim();
  if (!title) return res.status(400).json({ ok: false, error: 'title obrigatório' });
  const msgArg = message ? JSON.stringify(message) : '""';
  const args = [JSON.stringify(title), msgArg, JSON.stringify(source)];
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
    const svcStats       = nocExec('service_stats');
    const svcActive      = nocExec('whatsapp_active');
    const systemAutonomy = nocExec('system_autonomy');
    const insStats       = nocExec('insurance_stats');
    const insAlerts      = nocExec('insurance_alerts 5');

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
      service_stats:   svcStats,
      whatsapp_active: arr(svcActive),
      system_autonomy: systemAutonomy,
      insurance_stats:  insStats,
      insurance_alerts: arr(insAlerts),
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

// ── Service Billing ───────────────────────────────────────────────────────────
// Public validation page (no auth)
app.get('/validar/:token', (req, res) => {
  res.sendFile(path.join(__dirname, 'validar.html'));
});

// Public API: get service log by token
app.get('/api/service/validate/:token', (req, res) => {
  try {
    const token = req.params.token.replace(/[^a-zA-Z0-9\-]/g, '');
    const out   = execSync(`python3 /home/jdl/ai-os/bin/service_billing.py get_with_expenses ${token}`,
                           { timeout: 10000, encoding: 'utf8' });
    const d     = JSON.parse(out);
    if (!d || !d.id) return res.status(404).json({ error: 'not found' });
    res.json(d);
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// Public API: validate (approve/reject) — supports adjusted_days, extras, expense_decisions
app.post('/api/service/validate/:token', express.json(), (req, res) => {
  try {
    const token            = req.params.token.replace(/[^a-zA-Z0-9\-]/g, '');
    const approved         = req.body?.approved ? true : false;
    const note             = String(req.body?.note || '').slice(0, 500);
    const rejection_reason = String(req.body?.rejection_reason || '').trim().slice(0, 500);
    // Rejection reason obrigatório no lado do servidor
    if (!approved && !rejection_reason)
      return res.status(400).json({ ok: false, error: 'Motivo de rejeição obrigatório' });
    const flag = approved ? '--approve' : '--reject';
    const ip   = req.headers['x-forwarded-for']?.split(',')[0]?.trim() || req.socket.remoteAddress || '';
    const args = [
      'python3', '/home/jdl/ai-os/bin/service_billing.py',
      'validate', token, flag,
      '--note', note, '--ip', ip,
    ];
    if (!approved && rejection_reason) args.push('--rejection-reason', rejection_reason);
    if (req.body?.adjusted_days)       args.push('--adjusted-days', String(parseFloat(req.body.adjusted_days)));
    if (req.body?.extras)              args.push('--extras', JSON.stringify(req.body.extras));
    if (req.body?.expense_decisions)   args.push('--expense-decisions', JSON.stringify(req.body.expense_decisions));
    const { execFileSync } = require('child_process');
    const out = execFileSync(args[0], args.slice(1), { timeout: 20000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) {
    // Extract clean ValueError message from Python traceback
    const pyErr = String(e.stderr || e.message || e);
    const match = pyErr.match(/ValueError:\s*(.+)$/m);
    const errMsg = match ? match[1].trim() : pyErr.split('\n').filter(Boolean).pop() || 'Erro interno';
    res.status(400).json({ ok: false, error: errMsg });
  }
});

// Public: mark expense reimbursed via token
app.post('/api/service/validate/:token/expense/:eid/reimburse', express.json(), (req, res) => {
  try {
    const token = req.params.token.replace(/[^a-zA-Z0-9\-]/g, '');
    const eid   = parseInt(req.params.eid, 10);
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3',
      ['/home/jdl/ai-os/bin/service_billing.py', 'reimburse_expense', token, String(eid)],
      { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Public: serve expense photo via validation token
app.get('/api/service/validate/:token/expense/:eid/photo', (req, res) => {
  try {
    const token = req.params.token.replace(/[^a-zA-Z0-9\-]/g, '');
    const eid   = parseInt(req.params.eid, 10);
    // Verify token owns the expense
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/service_billing.py get_with_expenses ${token}`,
      { timeout: 8000, encoding: 'utf8' });
    const d = JSON.parse(out);
    if (!d?.id) return res.status(404).json({ error: 'not found' });
    const exp = (d.expenses||[]).find(e => e.id === eid);
    if (!exp || !exp.receipt_image_url) return res.status(404).json({ error: 'sem foto' });
    const photoPath = path.join('/home/jdl/ai-os/runtime/expenses', exp.receipt_image_url);
    if (!fs.existsSync(photoPath)) return res.status(404).json({ error: 'ficheiro não encontrado' });
    res.sendFile(photoPath);
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// Authenticated: list expenses for a timesheet by ID
app.get('/api/service/timesheets/:id/expenses', requireRole('operator'), (req, res) => {
  try {
    const ts_id = parseInt(req.params.id, 10);
    const out = execSync(`python3 -c "
import psycopg2,json,os
from psycopg2.extras import RealDictCursor
conn=psycopg2.connect(os.environ.get('DATABASE_URL','dbname=aios user=aios_user password=jdl host=127.0.0.1'),cursor_factory=RealDictCursor)
cur=conn.cursor()
cur.execute('SELECT id,worker_name,worker_phone_mbway,amount,expense_type,notes,receipt_name,receipt_nif_name,receipt_image_url,status,created_at FROM public.timesheet_expenses WHERE timesheet_id=%s ORDER BY id',(${ts_id},))
rows=[dict(r) for r in cur.fetchall()]
for r in rows:
    for k,v in r.items():
        if hasattr(v,'isoformat'): r[k]=v.isoformat()
conn.close()
print(json.dumps(rows))
"`, { timeout: 8000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Authenticated: validate endpoint also used by modal for expense fetch (via validate token route)
// Additional: get expenses by validate token (already handled by GET /api/service/validate/:token)
app.get('/api/service/validate/:token/expenses', (req, res) => {
  try {
    const token = req.params.token.replace(/[^a-zA-Z0-9\-]/g, '');
    const out = execSync(`python3 /home/jdl/ai-os/bin/service_billing.py get_with_expenses ${token}`,
                         { timeout: 8000, encoding: 'utf8' });
    const d = JSON.parse(out);
    res.json(d.expenses || []);
  } catch(e) { res.status(500).json({ error: String(e) }); }
});

// Authenticated: mark validated timesheet for revalidation (material change after validation)
app.post('/api/service/timesheets/:id/mark-revalidation', requireRole('operator'), express.json(), (req, res) => {
  try {
    const id     = parseInt(req.params.id, 10);
    const reason = String(req.body?.reason || '').trim().slice(0, 500);
    if (!id) return res.status(400).json({ ok: false, error: 'id inválido' });
    const { execFileSync } = require('child_process');
    const args = ['python3', '/home/jdl/ai-os/bin/service_billing.py', 'mark_revalidation', String(id)];
    if (reason) args.push('--reason', reason);
    const out = execFileSync(args[0], args.slice(1), { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || String(e)) }); }
});

// Authenticated: add expense to a timesheet
app.post('/api/service/timesheets/:id/expenses', requireRole('operator'), express.json(), (req, res) => {
  try {
    const ts_id = parseInt(req.params.id, 10);
    const { worker_id, worker_name, worker_phone_mbway, amount, expense_type,
            notes, receipt_name, receipt_nif_name, client_id } = req.body || {};
    if (!worker_id || !worker_name || !worker_phone_mbway || !amount)
      return res.status(400).json({ ok: false, error: 'worker_id, worker_name, phone_mbway e amount obrigatórios' });
    const { execFileSync } = require('child_process');
    const args = [
      '/home/jdl/ai-os/bin/service_billing.py', 'add_expense', String(ts_id),
      '--worker-id',   worker_id,
      '--worker-name', worker_name,
      '--phone-mbway', worker_phone_mbway,
      '--amount',      String(parseFloat(amount)),
      '--type',        expense_type || 'other',
    ];
    if (notes)            args.push('--notes',        notes);
    if (receipt_name)     args.push('--receipt-name', receipt_name);
    if (receipt_nif_name) args.push('--nif-name',     receipt_nif_name);
    if (client_id)        args.push('--client-id',    String(client_id));
    const out = execFileSync('python3', args, { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Authenticated: upload photo for expense
const _expenseDir = path.join('/home/jdl/ai-os/runtime/expenses');
if (!fs.existsSync(_expenseDir)) fs.mkdirSync(_expenseDir, { recursive: true });

const multer = (() => { try { return require('multer'); } catch(e) { return null; } })();
const _expUpload = multer ? multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => cb(null, _expenseDir),
    filename: (req, file, cb) => {
      const ext = path.extname(file.originalname) || '.jpg';
      cb(null, `${req.params.eid}_${Date.now()}${ext}`);
    },
  }),
  limits: { fileSize: 5 * 1024 * 1024 },
  fileFilter: (req, file, cb) => cb(null, /image\//i.test(file.mimetype)),
}) : null;

app.post('/api/service/expenses/:eid/photo', requireRole('operator'),
  ...[_expUpload ? _expUpload.single('photo') : (req, res, next) => { res.status(501).json({error:'multer unavailable'}); }],
  (req, res) => {
    if (!req.file) return res.status(400).json({ ok: false, error: 'ficheiro não enviado ou formato inválido' });
    const eid = parseInt(req.params.eid, 10);
    const relPath = req.file.filename;
    // Update receipt_image_url in DB
    try {
      execSync(
        `python3 -c "
import psycopg2, os
conn = psycopg2.connect(os.environ.get('DATABASE_URL','dbname=aios user=aios_user password=jdl host=127.0.0.1'))
conn.cursor().execute('UPDATE public.timesheet_expenses SET receipt_image_url=%s, updated_at=now() WHERE id=%s', ('${relPath}', ${eid}))
conn.commit(); conn.close()"`,
        { timeout: 5000 });
      res.json({ ok: true, expense_id: eid, file: relPath });
    } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
  }
);

// Authenticated: list logs + stats
app.get('/api/service/logs', requireRole('viewer'), (req, res) => {
  try {
    const status = req.query.status || '';
    const limit  = parseInt(req.query.limit) || 20;
    const args   = status ? `--status ${status} --limit ${limit}` : `--limit ${limit}`;
    const out    = execSync(`python3 /home/jdl/ai-os/bin/service_billing.py list ${args}`,
                            { timeout: 15000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/service/stats', requireRole('viewer'), (req, res) => {
  res.json(nocExec('service_stats'));
});

app.get('/api/service/active', requireRole('viewer'), (req, res) => {
  res.json(nocExec('whatsapp_active'));
});

app.get('/api/service/calendar', requireRole('viewer'), (req, res) => {
  const worker = req.query.worker || '';
  const months = req.query.months || '3';
  res.json(nocExec(`worker_calendar ${worker} ${months}`.trim()));
});

app.get('/api/system/autonomy', requireRole('viewer'), (req, res) => res.json(nocExec('system_autonomy')));
app.get('/api/jobs/by-role', requireRole('viewer'), (req, res) => res.json(nocExec('jobs_by_role')));
// ── Excel Export ──────────────────────────────────────────────────────────────
const EXPORT_TYPES = new Set(['insurance', 'ideas', 'decisions']);
const EXPORT_NAMES = { insurance: 'seguros', ideas: 'ideias', decisions: 'decisoes' };

app.get('/api/export/excel', requireRole('viewer'), (req, res) => {
  const type = String(req.query.type || '').toLowerCase();
  if (!EXPORT_TYPES.has(type))
    return res.status(400).json({ ok: false, error: `type inválido. Disponíveis: ${[...EXPORT_TYPES].join(', ')}` });
  try {
    const { execFileSync } = require('child_process');
    const data = execFileSync('python3', ['/home/jdl/ai-os/bin/export_excel.py', type], { timeout: 20000 });
    const ts   = new Date().toISOString().slice(0, 10);
    const name = `${EXPORT_NAMES[type]}_${ts}.xlsx`;
    res.setHeader('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet');
    res.setHeader('Content-Disposition', `attachment; filename="${name}"`);
    res.send(data);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 200) });
  }
});

app.get('/api/council/reviews', requireRole('viewer'), (req, res) => {
  const limit = parseInt(req.query.limit || '10');
  res.json(nocExec(`council_list ${limit}`));
});

app.post('/api/service/submit', requireRole('operator'), express.json(), (req, res) => {
  try {
    const { person_id, log_date, hours, location, car_used } = req.body || {};
    if (!person_id || !log_date || !hours || !location)
      return res.status(400).json({ ok: false, error: 'person_id, log_date, hours, location required' });
    const safeLoc = String(location).replace(/'/g, "'\\''").slice(0, 200);
    const carFlag = car_used ? ' --car' : '';
    const out     = execSync(
      `python3 /home/jdl/ai-os/bin/service_billing.py submit ${person_id} ${log_date} ${hours} '${safeLoc}'${carFlag}`,
      { timeout: 15000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// ── Commercial Quotes ─────────────────────────────────────────────────────────

app.get('/comercial', (req, res) => res.sendFile(path.join(__dirname, 'comercial.html')));

app.get('/api/commercial/requests', requireRole('viewer'), (req, res) => {
  const status = req.query.status || '';
  const limit  = parseInt(req.query.limit) || 20;
  res.json(nocExec(`commercial_requests ${status} ${limit}`.trim()));
});

app.get('/api/commercial/requests/:id', requireRole('viewer'), (req, res) => {
  const id = parseInt(req.params.id) || 0;
  res.json(nocExec(`commercial_request_get ${id}`));
});

app.post('/api/commercial/requests', requireRole('operator'), (req, res) => {
  try {
    const { raw_request, source } = req.body || {};
    if (!raw_request) return res.status(400).json({ ok: false, error: 'raw_request obrigatório' });
    const safeRaw = String(raw_request).slice(0, 4000).replace(/'/g, "'\\''");
    const safeSrc = ['manual','email','whatsapp','form'].includes(source) ? source : 'manual';
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py submit '${safeRaw}' --source ${safeSrc}`,
      { timeout: 15000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/commercial/requests/:id/quote', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id) || 0;
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py quote ${id}`,
      { timeout: 30000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/commercial/quotes/:id', requireRole('viewer'), (req, res) => {
  const id = parseInt(req.params.id) || 0;
  res.json(nocExec(`commercial_quote_get ${id}`));
});

app.post('/api/commercial/quotes/:id/approve', requireRole('supervisor', 'admin'), (req, res) => {
  try {
    const id = parseInt(req.params.id) || 0;
    const by = String(req.body?.approved_by || req.user?.username || 'admin').replace(/[^a-zA-Z0-9_ ]/g, '').slice(0, 80);
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py approve ${id} --by ${JSON.stringify(by)}`,
      { timeout: 10000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.put('/api/commercial/quotes/:id', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id) || 0;
    const { line_items, assumptions, exclusions } = req.body || {};
    const payload = JSON.stringify({ line_items, assumptions, exclusions });
    const safePayload = payload.replace(/'/g, "'\\''");
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py update_quote ${id} '${safePayload}'`,
      { timeout: 10000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.post('/api/commercial/quotes/:id/send', requireRole('supervisor', 'admin'), (req, res) => {
  try {
    const id = parseInt(req.params.id) || 0;
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py send ${id}`,
      { timeout: 30000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.get('/api/commercial/quotes/:id/pdf', requireRole('viewer'), (req, res) => {
  try {
    const id = parseInt(req.params.id) || 0;
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/commercial_engine.py pdf ${id}`,
      { timeout: 30000, encoding: 'utf8' }
    );
    const result = JSON.parse(out);
    if (!result.ok || !result.pdf_path) return res.status(404).json(result);
    res.download(result.pdf_path);
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

app.get('/api/commercial/stats', requireRole('viewer'), (req, res) => {
  res.json(nocExec('commercial_stats'));
});

// ── Insurance Module ──────────────────────────────────────────────────────────

app.get('/api/insurance/policies', requireRole('viewer'), (req, res) => {
  const status = (req.query.status || '').replace(/[^a-z_]/gi, '');
  const type   = (req.query.type   || '').replace(/[^a-z_]/gi, '');
  const limit  = Math.max(1, Math.min(200, parseInt(req.query.limit) || 50));
  res.json(nocExec(`insurance_policies ${status} ${type} ${limit}`.trim()));
});

app.get('/api/insurance/policies/:id', requireRole('viewer'), (req, res) => {
  const id = parseInt(req.params.id);
  if (!id) return res.status(400).json({ error: 'id inválido' });
  res.json(nocExec(`insurance_policy_get ${id}`));
});

app.post('/api/insurance/policies', requireRole('operator'), (req, res) => {
  try {
    const b = req.body || {};
    const args = [
      '--insurer',        JSON.stringify(String(b.insurer_name || b.insurer || '')),
      '--entity-type',    JSON.stringify(String(b.entity_type  || 'company')),
      '--entity-ref',     JSON.stringify(String(b.entity_ref   || '')),
      '--policy-number',  JSON.stringify(String(b.policy_number || '')),
      '--category',       JSON.stringify(String(b.category     || '')),
      '--status',         JSON.stringify(String(b.status       || 'active')),
    ];
    if (b.start_date)     args.push('--start',   JSON.stringify(String(b.start_date)));
    if (b.end_date)       args.push('--end',     JSON.stringify(String(b.end_date)));
    if (b.renewal_date)   args.push('--renewal', JSON.stringify(String(b.renewal_date)));
    if (b.premium_amount) args.push('--premium', parseFloat(b.premium_amount));
    if (b.notes)          args.push('--notes',   JSON.stringify(String(b.notes).slice(0,500)));
    const out = require('child_process').execSync(
      `python3 /home/jdl/ai-os/bin/insurance_engine.py add ${args.join(' ')}`,
      { timeout: 10000, encoding: 'utf8' }
    );
    const result = JSON.parse(out);
    // Gerar alertas automáticos (fire-and-forget)
    require('child_process').exec('python3 /home/jdl/ai-os/bin/insurance_engine.py generate-alerts');
    res.json(result);
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

app.put('/api/insurance/policies/:id', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    const b = req.body || {};
    const args = [id];
    if (b.insurer_name)   args.push('--insurer',  JSON.stringify(String(b.insurer_name)));
    if (b.end_date)       args.push('--end',      JSON.stringify(String(b.end_date)));
    if (b.renewal_date)   args.push('--renewal',  JSON.stringify(String(b.renewal_date)));
    if (b.status)         args.push('--status',   JSON.stringify(String(b.status)));
    if (b.notes)          args.push('--notes',    JSON.stringify(String(b.notes).slice(0,500)));
    if (b.premium_amount) args.push('--premium',  parseFloat(b.premium_amount));
    const out = require('child_process').execSync(
      `python3 /home/jdl/ai-os/bin/insurance_engine.py update ${args.join(' ')}`,
      { timeout: 10000, encoding: 'utf8' }
    );
    const result = JSON.parse(out);
    require('child_process').exec('python3 /home/jdl/ai-os/bin/insurance_engine.py generate-alerts');
    res.json(result);
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

app.get('/api/insurance/alerts', requireRole('viewer'), (req, res) => {
  const limit = Math.max(1, Math.min(200, parseInt(req.query.limit) || 50));
  res.json(nocExec(`insurance_alerts ${limit}`));
});

app.post('/api/insurance/alerts/:id/resolve', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    const out = require('child_process').execSync(
      `python3 -c "
import sys; sys.path.insert(0,'/home/jdl/ai-os/bin')
from insurance_engine import _conn, resolve_alert
e,t = _conn()
import json; print(json.dumps(resolve_alert(e,t,${id})))
"`, { timeout: 8000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

app.get('/api/insurance/stats', requireRole('viewer'), (req, res) => {
  res.json(nocExec('insurance_stats'));
});

app.post('/api/insurance/generate-alerts', requireRole('operator'), (req, res) => {
  try {
    const out = require('child_process').execSync(
      'python3 /home/jdl/ai-os/bin/insurance_engine.py generate-alerts',
      { timeout: 15000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

app.post('/api/insurance/ingest', requireRole('operator'), (req, res) => {
  try {
    const { file_base64, filename } = req.body || {};
    if (!file_base64 || !filename) return res.status(400).json({ error: 'file_base64 e filename obrigatórios' });
    const safeFilename = String(filename).replace(/[^a-zA-Z0-9_.\-]/g, '_').slice(0, 100);
    const dest = `/home/jdl/ai-os/runtime/insurance/${safeFilename}`;
    require('fs').writeFileSync(dest, Buffer.from(file_base64, 'base64'));
    const out = require('child_process').execSync(
      `python3 /home/jdl/ai-os/bin/insurance_engine.py ingest ${JSON.stringify(dest)}`,
      { timeout: 30000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

app.post('/api/insurance/policies/:id/doc', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    const b = req.body || {};
    const docType = (b.doc_type || 'policy').replace(/[^a-z_]/gi, '');
    const amount  = b.amount ? parseFloat(b.amount) : '';
    const out = require('child_process').execSync(
      `python3 /home/jdl/ai-os/bin/insurance_engine.py add-doc ${id} --type ${docType}${amount ? ' --amount '+amount : ''}`,
      { timeout: 10000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,300) }); }
});

// Serve PDF de documento de apólice
app.get('/api/insurance/docs/:docId/pdf', requireRole('viewer'), (req, res) => {
  try {
    const docId = parseInt(req.params.docId);
    if (!docId || isNaN(docId)) return res.status(400).json({ error: 'id inválido' });
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/noc_query.py insurance_doc_path ${docId}`,
      { timeout: 5000, encoding: 'utf8' }).trim();
    if (!out || !fs.existsSync(out)) return res.status(404).json({ error: 'ficheiro não encontrado' });
    res.sendFile(path.resolve(out));
  } catch(e) { res.status(500).json({ error: String(e.message || e).slice(0,200) }); }
});

// ── Mock Invoice ───────────────────────────────────────────────────────────────

app.post('/api/finance/invoice/mock', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const { client, description, net, vat_rate, items, email_to } = req.body || {};
    if (!client || !net) return res.status(400).json({ ok: false, error: 'client e net obrigatorios' });
    const args = JSON.stringify({ client, description: description || '', net: parseFloat(net),
      vat_rate: parseFloat(vat_rate || 23), items: items || [], email_to: email_to || '' });
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', [
      '/home/jdl/ai-os/bin/invoice_mock.py', 'generate_api', args
    ], { timeout: 15000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 300) }); }
});

app.post('/api/finance/invoice/mock/draft_ts/:tsId', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const tsId = parseInt(req.params.tsId, 10);
    if (!tsId) return res.status(400).json({ ok: false, error: 'tsId invalido' });
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3',
      ['/home/jdl/ai-os/bin/invoice_mock.py', 'draft_ts', String(tsId)],
      { timeout: 15000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 300) }); }
});

app.get('/api/finance/invoice/mock/drafts', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['/home/jdl/ai-os/bin/invoice_mock.py', 'drafts_api'],
      { timeout: 10000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 200) }); }
});

app.post('/api/finance/invoice/mock/:id/send', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const id    = parseInt(req.params.id, 10);
    const email = String(req.body?.email || '').trim();
    if (!id || !email) return res.status(400).json({ ok: false, error: 'id e email obrigatorios' });
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3',
      ['/home/jdl/ai-os/bin/invoice_mock.py', 'send_draft_api', String(id), email],
      { timeout: 20000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 300) }); }
});

app.get('/api/finance/invoice/mock/list', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['/home/jdl/ai-os/bin/invoice_mock.py', 'list_api'],
      { timeout: 10000 }).toString();
    res.json(JSON.parse(out));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 200) }); }
});

app.get('/api/finance/invoice/mock/:id/pdf', requireRole(...FINANCE_ROLES), (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['/home/jdl/ai-os/bin/invoice_mock.py', 'get_pdf', String(id)],
      { timeout: 10000 }).toString();
    const r = JSON.parse(out);
    if (!r.ok || !r.pdf_path) return res.status(404).json({ ok: false, error: 'PDF nao encontrado' });
    res.sendFile(r.pdf_path);
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message || e).slice(0, 200) }); }
});

// ── Viaturas ──────────────────────────────────────────────────────────────────

app.put('/api/vehicles/:id', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    const allowed = ['inspecao_data','inspecao_proxima','iuc_data','iuc_valor',
                     'seguro_apolice','seguro_validade','km_atual','notes','estado'];
    const sets = []; const vals = [];
    for (const k of allowed) {
      if (req.body[k] !== undefined) { sets.push(`${k}=$${sets.length+1}`); vals.push(req.body[k] || null); }
    }
    if (!sets.length) return res.status(400).json({ ok: false, error: 'Nada para actualizar' });
    vals.push(id);
    const { Client } = require('pg');
    const client = new Client({ host:'127.0.0.1', database:'aios', user:'aios_user', password:'jdl' });
    client.connect();
    client.query(
      `UPDATE public.vehicles SET ${sets.join(',')}, updated_at=NOW() WHERE id=$${vals.length} RETURNING id`,
      vals, (err, r) => {
        client.end();
        if (err) return res.status(500).json({ ok: false, error: err.message });
        res.json({ ok: true, id: r.rows[0]?.id });
      });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e) }); }
});

// ── System Config ─────────────────────────────────────────────────────────────

app.get('/api/sysconfig', requireRole('admin'), (req, res) => {
  const cat = req.query.category || '';
  res.json(nocExec(`sysconfig_list ${cat}`.trim()));
});

app.get('/api/sysconfig/:key', requireRole('admin'), (req, res) => {
  res.json(nocExec(`sysconfig_get ${req.params.key}`));
});

app.post('/api/sysconfig/:key', requireRole('admin'), (req, res) => {
  try {
    const key   = req.params.key.replace(/[^a-z0-9_]/gi, '');
    const value = String(req.body?.value ?? '').slice(0, 2000);
    const by    = String(req.user?.username || 'admin').replace(/[^a-zA-Z0-9_ ]/g, '').slice(0, 80);
    const safeV = value.replace(/'/g, "'\\''");
    const out   = execSync(
      `python3 /home/jdl/ai-os/bin/noc_query.py sysconfig_set ${key} '${safeV}' '${by}'`,
      { timeout: 5000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// ── Inbox (Aprovações unificadas) ────────────────────────────────────────────
app.get('/api/inbox/pending', requireRole('viewer'), (req, res) => {
  res.json(nocExec('inbox_pending'));
});

// ── Agent Inbox ───────────────────────────────────────────────────────────────
app.get('/api/agent-inbox', requireRole('viewer'), (req, res) => {
  const status = req.query.status || 'pending';
  const limit  = parseInt(req.query.limit) || 50;
  res.json(nocExec(`agent_inbox_list ${status} ${limit}`));
});

app.post('/api/agent-inbox', requireRole('operator'), express.json(), (req, res) => {
  try {
    const { body, target = 'local', source = 'ui', sender = '' } = req.body || {};
    if (!body) return res.status(400).json({ ok: false, error: 'body required' });
    const safeBody   = String(body).slice(0, 2000).replace(/"/g, '\\"');
    const safeTarget = String(target).replace(/[^a-z]/g, '');
    const safeSrc    = String(source).replace(/[^a-z_]/g, '');
    const safeSender = String(sender).slice(0, 80).replace(/"/g, '');
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/noc_query.py agent_inbox_add "${safeBody}" "${safeTarget}" "${safeSrc}" "${safeSender}"`,
      { timeout: 10000, encoding: 'utf8' }
    );
    const result = JSON.parse(out);
    // Fire-and-forget: processar imediatamente sem esperar pelo timer (30s)
    if (safeTarget === 'claude')
      require('child_process').exec('python3 /home/jdl/ai-os/bin/prompt_inbox_worker.py');
    res.json(result);
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

app.patch('/api/agent-inbox/:id', requireRole('operator'), express.json(), (req, res) => {
  try {
    const id     = parseInt(req.params.id);
    const status = String(req.body?.status || 'done').replace(/[^a-z]/g, '');
    const result = String(req.body?.result || '').slice(0, 1000).replace(/"/g, '\\"');
    const out    = execSync(
      `python3 /home/jdl/ai-os/bin/noc_query.py agent_inbox_update ${id} ${status} "${result}"`,
      { timeout: 10000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// ── Autonomia: blocked review, approve, reject ────────────────────────────────
app.get('/api/autonomia/blocked', requireRole('operator'), (req, res) => {
  res.json(nocExec('autonomia_blocked 20'));
});

app.post('/api/autonomia/jobs/:id/approve', requireRole('operator'), (req, res) => {
  const id       = parseInt(req.params.id);
  const approver = String(req.user?.username || req.user?.sub || 'operator').replace(/[^a-zA-Z0-9_\-.]/g, '');
  res.json(nocExec(`autonomia_approve ${id} ${approver}`));
});

app.post('/api/autonomia/jobs/:id/reject', requireRole('operator'), (req, res) => {
  const id = parseInt(req.params.id);
  res.json(nocExec(`autonomia_reject ${id}`));
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
    const { topic, kind, context } = req.body || {};
    if (!topic) return res.status(400).json({ ok: false, error: 'topic required' });
    const safeTopic   = String(topic).replace(/'/g, "'\\''").slice(0, 500);
    const safeKind    = ['idea','decision','project','architecture','problem','general'].includes(kind) ? kind : 'general';
    const safeContext = context ? ` --context '${String(context).replace(/'/g,"'\\''").slice(0,800)}'` : '';
    const out = execSync(
      `python3 /home/jdl/ai-os/bin/council.py analyze '${safeTopic}' --kind ${safeKind}${safeContext}`,
      { timeout: 120000, encoding: 'utf8' }
    );
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e.stderr || e.message || e).slice(0,400) }); }
});

app.get('/api/council/:id', requireRole('viewer'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    const out = execSync(`python3 /home/jdl/ai-os/bin/council.py get ${id}`, { timeout: 10000, encoding: 'utf8' });
    res.json(JSON.parse(out));
  } catch(e) { res.json({ ok: false, error: String(e.message || e).slice(0,300) }); }
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

// ── WhatsApp Ponto (Twilio webhook) ───────────────────────────────────────────

// Inbound: worker envia "inicio"/"fim" via WhatsApp
app.post('/api/whatsapp/inbound', express.urlencoded({ extended: false }), (req, res) => {
  const from        = (req.body.From        || '').replace(/^whatsapp:/, '');
  const body        = (req.body.Body        || '').trim();
  const lat         = req.body.Latitude     || '';
  const lon         = req.body.Longitude    || '';
  const addr        = (req.body.Address     || '').replace(/'/g, "\\'");
  const profileName = (req.body.ProfileName || '').trim();

  const sid   = req.body.MessageSid || '';
  const pyArgs = [
    '/home/jdl/ai-os/bin/whatsapp_handler.py',
    '--from', from,
    '--body', body,
    '--sid',  sid,
  ];
  if (lat) pyArgs.push('--lat', lat, '--lon', lon);
  if (addr) pyArgs.push('--addr', addr);
  if (profileName) pyArgs.push('--profile-name', profileName);

  let stdout = '', stderr = '';
  const { spawn } = require('child_process');
  const p = spawn('python3', pyArgs, {
    env: { ...process.env },
    timeout: 12000,
  });
  p.stdout.on('data', d => stdout += d);
  p.stderr.on('data', d => stderr += d);
  p.on('close', () => {
    let reply = '';
    try { reply = JSON.parse(stdout).reply || ''; } catch(e) {
      console.error('[whatsapp] parse error:', stderr.slice(0, 200));
    }
    const safe = reply
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
    res.set('Content-Type', 'text/xml');
    res.send(
      '<?xml version="1.0" encoding="UTF-8"?><Response>' +
      (safe ? `<Message>${safe}</Message>` : '') +
      '</Response>'
    );
  });
});

// Status callback — Twilio envia updates de entrega (delivered, read, failed)
app.post('/api/whatsapp/status', express.urlencoded({ extended: false }), (req, res) => {
  const { MessageSid, MessageStatus, To, ErrorCode } = req.body;
  const fs = require('fs');
  const logLine = JSON.stringify({
    ts: new Date().toISOString(),
    sid: MessageSid, status: MessageStatus, to: To, error: ErrorCode || null,
  }) + '\n';
  fs.appendFile('/home/jdl/ai-os/runtime/whatsapp/status.log', logLine, () => {});

  // 63016: free-form fora da janela 24h → fallback email assíncrono
  if (ErrorCode === '63016' && MessageSid) {
    const { execFile } = require('child_process');
    execFile('python3', [
      '/home/jdl/ai-os/bin/whatsapp_fallback.py',
      '--sid', MessageSid,
      '--to', (To || '').replace(/^whatsapp:/, ''),
    ], { timeout: 15000 }, (err, stdout) => {
      if (err) console.error('[wa/status] fallback error:', err.message);
      else if (stdout.trim()) console.log('[wa/status] fallback:', stdout.trim());
    });
  }

  res.status(204).end();
});

// Health check
app.get('/api/whatsapp/health', (req, res) => {
  const sid  = String(process.env.TWILIO_ACCOUNT_SID || '');
  const from = process.env.TWILIO_WHATSAPP_FROM || process.env.TWILIO_WHATSAPP_NUMBER || null;
  res.json({
    ok:         true,
    configured: sid.startsWith('AC') && !!from,
    mode:       process.env.WHATSAPP_MODE || 'sandbox',
    from,
  });
});

// Últimos inbound WhatsApp (admin)
app.get('/api/admin/whatsapp/inbound-recent', requireRole('operator'), async (req, res) => {
  try {
    const limit = Math.min(parseInt(req.query.limit || '20', 10), 100);
    const rows = await _pgQuery(`
      SELECT id, from_phone, profile_name, body, client_id, watched,
             received_at
      FROM public.whatsapp_inbound_log
      ORDER BY received_at DESC
      LIMIT $1
    `, [limit]);
    res.json({ ok: true, rows });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e.message) });
  }
});

// ── Workspace ────────────────────────────────────────────────────────────────
const WS_PY = '/home/jdl/ai-os/bin/workspace_engine.py';
const { execFileSync: _execFile } = require('child_process');

function wsExec(args, timeout = 10000) {
  try {
    const out = _execFile('python3', [WS_PY, ...args], { timeout, encoding: 'utf8' });
    return JSON.parse(out);
  } catch(e) {
    return { error: (e.stderr || e.message || String(e)).slice(0, 400) };
  }
}

app.get('/api/workspace/sessions', requireRole('viewer'), (req, res) => {
  res.json(wsExec(['list_sessions']));
});

app.post('/api/workspace/sessions', requireRole('viewer'), (req, res) => {
  const title = String(req.body.title || 'Nova sessão').slice(0, 200);
  const agent = String(req.body.agent || 'sonnet');
  res.json(wsExec(['new_session', title, agent]));
});

app.get('/api/workspace/sessions/:id', requireRole('viewer'), (req, res) => {
  res.json(wsExec(['get_session', req.params.id]));
});

app.delete('/api/workspace/sessions/:id', requireRole('viewer'), (req, res) => {
  res.json(wsExec(['delete_session', req.params.id]));
});

app.post('/api/workspace/sessions/:id/chat', requireRole('viewer'), (req, res) => {
  const message = String(req.body.message || '').trim();
  const agent   = String(req.body.agent || 'sonnet');
  if (!message) return res.status(400).json({ error: 'Mensagem vazia' });
  const result = wsExec(['chat', req.params.id, agent, message], 120000);
  res.json(result);
});

// ── Client portal routes (/conta.html) ───────────────────────────────────────
// Serve /conta as static HTML (requires login with client role)
app.get('/conta', (req, res) => {
  res.sendFile(path.join(__dirname, 'conta.html'));
});

// ── Clients CRUD (admin only) ─────────────────────────────────────────────────
app.get('/api/clients', requireRole('admin'), (req, res) => {
  res.json(nocExec('clients_list'));
});

app.post('/api/clients', requireRole('admin'), (req, res) => {
  const { name, nif, contact_email, contact_phone, notes } = req.body || {};
  if (!name) return res.status(400).json({ ok: false, error: 'name obrigatório' });
  const safe = (s) => String(s || '').replace(/'/g, "''");
  const out = execSync(
    `python3 -c "
import psycopg2, json, os
dsn = os.environ.get('DATABASE_URL','dbname=aios user=aios_user password=jdl host=127.0.0.1')
conn = psycopg2.connect(dsn)
cur = conn.cursor()
cur.execute('''INSERT INTO public.clients (name,nif,contact_email,contact_phone,notes) VALUES (%s,%s,%s,%s,%s) RETURNING id''',
  ('${safe(name)}','${safe(nif)}','${safe(contact_email)}','${safe(contact_phone)}','${safe(notes)}'))
row = cur.fetchone()
conn.commit()
conn.close()
print(json.dumps({'ok':True,'id':row[0]}))
"`,
    { timeout: 8000, encoding: 'utf8', env: { ...process.env, DATABASE_URL: process.env.DATABASE_URL || '' } }
  );
  try { res.json(JSON.parse(out)); } catch(e) { res.json({ ok: false, error: String(e) }); }
});

// ── Client dashboard API (/api/client/*) ──────────────────────────────────────
// ── Admin Invoices (client_invoices) ─────────────────────────────────────────

app.get('/api/admin/invoices', requireRole('finance'), async (req, res) => {
  try {
    const { status, client_id } = req.query;
    const params = [];
    let where = 'WHERE 1=1';
    if (status)    { params.push(status);    where += ` AND ci.status=$${params.length}`; }
    if (client_id) { params.push(client_id); where += ` AND ci.client_id=$${params.length}`; }
    const rows = await _pgQuery(`
      SELECT ci.id, ci.client_id, c.name AS client_name,
             ci.timesheet_id, ci.invoice_number, ci.status,
             ci.issue_date, ci.due_date, ci.subtotal, ci.tax_total, ci.total,
             ci.sent_at, ci.paid_at, ci.created_at,
             et.worker_id, et.log_date
      FROM public.client_invoices ci
      LEFT JOIN public.clients c ON c.id = ci.client_id
      LEFT JOIN public.event_timesheets et ON et.id = ci.timesheet_id
      ${where}
      ORDER BY ci.created_at DESC LIMIT 200
    `, params);
    res.json({ ok: true, invoices: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.get('/api/admin/invoices/:id', requireRole('finance'), async (req, res) => {
  try {
    const id = req.params.id;
    const [inv, lines, payments] = await Promise.all([
      _pgQuery(`
        SELECT ci.*, c.name AS client_name, et.worker_id, et.log_date, et.hours
        FROM public.client_invoices ci
        LEFT JOIN public.clients c ON c.id = ci.client_id
        LEFT JOIN public.event_timesheets et ON et.id = ci.timesheet_id
        WHERE ci.id = $1
      `, [id]),
      _pgQuery(`SELECT * FROM public.client_invoice_lines WHERE invoice_id=$1 ORDER BY id`, [id]),
      _pgQuery(`SELECT * FROM public.payments_received WHERE invoice_id=$1 ORDER BY paid_at`, [id]),
    ]);
    if (!inv.length) return res.status(404).json({ ok: false, error: 'not found' });
    res.json({ ok: true, invoice: inv[0], lines, payments });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/invoices/:id/issue', requireRole('finance'), async (req, res) => {
  try {
    const { invoice_number, due_days = 30 } = req.body;
    const [existing] = await _pgQuery(
      `SELECT id, status FROM public.client_invoices WHERE id=$1`, [req.params.id]);
    if (!existing) return res.status(404).json({ ok: false, error: 'not found' });
    if (existing.status !== 'invoice_draft')
      return res.status(400).json({ ok: false, error: `status is ${existing.status}, expected invoice_draft` });
    const seq = await _pgQuery(
      `SELECT COALESCE(MAX(id),0)+1 AS n FROM public.client_invoices`);
    const num = invoice_number || `JDL-${new Date().getFullYear()}-${String(seq[0].n).padStart(4,'0')}`;
    await _pgQuery(`
      UPDATE public.client_invoices
      SET status='invoiced', invoice_number=$1,
          issue_date=CURRENT_DATE, due_date=CURRENT_DATE + $2::int,
          sent_at=now()
      WHERE id=$3
    `, [num, due_days, req.params.id]);
    console.log(JSON.stringify({ event:'invoice_issued', invoice_id:req.params.id, number:num }));
    res.json({ ok: true, invoice_number: num });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/invoices/:id/mark-paid', requireRole('finance'), async (req, res) => {
  try {
    const { amount, method = 'bank_transfer', reference = '', notes = '' } = req.body;
    const [inv] = await _pgQuery(
      `SELECT id, total, status FROM public.client_invoices WHERE id=$1`, [req.params.id]);
    if (!inv) return res.status(404).json({ ok: false, error: 'not found' });
    const paid_amount = parseFloat(amount || inv.total);
    await _pgQuery(`
      INSERT INTO public.payments_received (invoice_id, amount, method, reference, notes)
      VALUES ($1, $2, $3, $4, $5)
    `, [req.params.id, paid_amount, method, reference, notes]);
    await _pgQuery(`
      UPDATE public.client_invoices SET status='paid', paid_at=now() WHERE id=$1
    `, [req.params.id]);
    console.log(JSON.stringify({ event:'invoice_marked_paid', invoice_id:req.params.id, amount:paid_amount }));
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

// ── Admin Cashflow ────────────────────────────────────────────────────────────

app.get('/api/admin/cashflow/overview', requireRole('finance'), async (req, res) => {
  try {
    const [invoiced, received, receivable, payPending, payPaid] = await Promise.all([
      _pgQuery(`
        SELECT COALESCE(SUM(total),0) AS v FROM public.client_invoices
        WHERE status IN ('invoiced','paid')
          AND DATE_TRUNC('month', created_at) = DATE_TRUNC('month', NOW())
      `, []),
      _pgQuery(`
        SELECT COALESCE(SUM(amount),0) AS v FROM public.payments_received
        WHERE DATE_TRUNC('month', paid_at) = DATE_TRUNC('month', NOW())
      `, []),
      _pgQuery(`
        SELECT COALESCE(SUM(total),0) AS v FROM public.client_invoices
        WHERE status='invoiced'
      `, []),
      _pgQuery(`
        SELECT COALESCE(SUM(amount),0) AS v FROM public.worker_payouts
        WHERE status='pending'
      `, []),
      _pgQuery(`
        SELECT COALESCE(SUM(amount),0) AS v FROM public.worker_payouts
        WHERE status='paid'
          AND DATE_TRUNC('month', paid_at) = DATE_TRUNC('month', NOW())
      `, []),
    ]);
    const inv   = parseFloat(invoiced[0].v);
    const recv  = parseFloat(received[0].v);
    const open  = parseFloat(receivable[0].v);
    const ppend = parseFloat(payPending[0].v);
    const ppaid = parseFloat(payPaid[0].v);
    console.log(JSON.stringify({ event: 'cashflow_overview_requested', user: req.user?.sub }));
    res.json({
      ok: true,
      total_invoiced_month:   inv,
      total_received_month:   recv,
      total_receivable_open:  open,
      total_payout_pending:   ppend,
      total_payout_paid:      ppaid,
      estimated_margin:       parseFloat((inv - ppend).toFixed(2)),
      operational_balance:    parseFloat((recv - ppaid).toFixed(2)),
    });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.get('/api/admin/payouts', requireRole('finance'), async (req, res) => {
  try {
    const { status } = req.query;
    const params = [];
    let where = 'WHERE 1=1';
    if (status) { params.push(status); where += ` AND wp.status=$${params.length}`; }
    const rows = await _pgQuery(`
      SELECT wp.id, wp.timesheet_id, wp.worker_id, wp.client_id,
             c.name AS client_name, wp.amount, wp.status,
             wp.due_date, wp.paid_at, wp.payment_method, wp.notes, wp.created_at,
             et.log_date, et.hours
      FROM public.worker_payouts wp
      LEFT JOIN public.clients c ON c.id = wp.client_id
      LEFT JOIN public.event_timesheets et ON et.id = wp.timesheet_id
      ${where}
      ORDER BY wp.created_at DESC LIMIT 200
    `, params);
    res.json({ ok: true, payouts: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/payouts/:id/mark-paid', requireRole('finance'), async (req, res) => {
  try {
    const { method = 'bank_transfer', notes = '' } = req.body;
    const [wp] = await _pgQuery(
      `SELECT id, status FROM public.worker_payouts WHERE id=$1`, [req.params.id]);
    if (!wp) return res.status(404).json({ ok: false, error: 'not found' });
    if (wp.status === 'paid') return res.status(400).json({ ok: false, error: 'already paid' });
    await _pgQuery(`
      UPDATE public.worker_payouts
      SET status='paid', paid_at=now(), payment_method=$1, notes=$2
      WHERE id=$3
    `, [method, notes || null, req.params.id]);
    console.log(JSON.stringify({
      event: 'worker_payout_marked_paid', payout_id: req.params.id,
      method, user: req.user?.sub,
    }));
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

// ── Admin Marketplace ─────────────────────────────────────────────────────────

app.get('/api/admin/marketplace/jobs', requireRole('operator'), async (req, res) => {
  try {
    const { status } = req.query;
    const params = [];
    let where = 'WHERE 1=1';
    if (status) { params.push(status); where += ` AND mj.status=$${params.length}`; }
    const rows = await _pgQuery(`
      SELECT mj.id, mj.title, mj.location, mj.starts_at, mj.ends_at,
             mj.needed_workers, mj.role_required, mj.billing_model, mj.status, mj.notes,
             mj.service_job_id, mj.created_at, c.name AS client_name,
             COUNT(ma.id) FILTER (WHERE ma.status != 'expired')                      AS total_apps,
             COUNT(ma.id) FILTER (WHERE ma.status IN ('invited'))                    AS invited_count,
             COUNT(ma.id) FILTER (WHERE ma.status = 'accepted')                      AS accepted_count,
             COUNT(ma.id) FILTER (WHERE ma.status = 'selected')                      AS selected_count
      FROM public.marketplace_jobs mj
      LEFT JOIN public.clients c ON c.id = mj.client_id
      LEFT JOIN public.marketplace_applications ma ON ma.job_id = mj.id
      ${where}
      GROUP BY mj.id, c.name
      ORDER BY mj.starts_at ASC LIMIT 200
    `, params);
    res.json({ ok: true, jobs: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/marketplace/jobs', requireRole('operator'), async (req, res) => {
  try {
    const { client_id, title, location, starts_at, ends_at,
            needed_workers = 1, role_required, billing_model = 'marketplace_direct', notes } = req.body;
    if (!client_id || !title || !starts_at || !ends_at)
      return res.status(400).json({ ok: false, error: 'client_id, title, starts_at, ends_at obrigatórios' });
    if (new Date(ends_at) <= new Date(starts_at))
      return res.status(400).json({ ok: false, error: 'ends_at deve ser posterior a starts_at' });
    const [row] = await _pgQuery(`
      INSERT INTO public.marketplace_jobs
        (client_id, title, location, starts_at, ends_at, needed_workers, role_required, billing_model, notes)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
      RETURNING id
    `, [client_id, title, location||null, starts_at, ends_at, needed_workers,
        role_required||null, billing_model, notes||null]);
    await _mkAudit('marketplace_job_created', row.id, { title, client_id });
    console.log(JSON.stringify({ event: 'marketplace_job_created', job_id: row.id, title, client_id }));
    res.json({ ok: true, job_id: row.id });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.get('/api/admin/marketplace/jobs/:id', requireRole('operator'), async (req, res) => {
  try {
    const [job, apps] = await Promise.all([
      _pgQuery(`
        SELECT mj.*, c.name AS client_name
        FROM public.marketplace_jobs mj
        LEFT JOIN public.clients c ON c.id = mj.client_id
        WHERE mj.id=$1
      `, [req.params.id]),
      _pgQuery(`
        SELECT ma.*, p.name AS worker_name, mwp.whatsapp_phone, mwp.rating
        FROM public.marketplace_applications ma
        JOIN public.persons p ON p.id = ma.worker_id
        LEFT JOIN public.marketplace_worker_profiles mwp ON mwp.worker_id = ma.worker_id
        WHERE ma.job_id=$1
        ORDER BY ma.score DESC, ma.created_at
      `, [req.params.id]),
    ]);
    if (!job.length) return res.status(404).json({ ok: false, error: 'not found' });
    res.json({ ok: true, job: job[0], applications: apps });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/marketplace/jobs/:id/invite', requireRole('operator'), async (req, res) => {
  try {
    const { worker_id, notes, send_whatsapp = false } = req.body;
    if (!worker_id) return res.status(400).json({ ok: false, error: 'worker_id obrigatório' });
    const [job] = await _pgQuery(
      `SELECT id, starts_at, ends_at, status FROM public.marketplace_jobs WHERE id=$1`, [req.params.id]);
    if (!job) return res.status(404).json({ ok: false, error: 'job not found' });
    if (job.status !== 'open')
      return res.status(400).json({ ok: false, error: `job não está aberto (status=${job.status})` });

    // Conflito: worker já convidado/selecionado neste job
    const [dup] = await _pgQuery(`
      SELECT id FROM public.marketplace_applications
      WHERE job_id=$1 AND worker_id=$2 AND status NOT IN ('declined','rejected','expired')
    `, [req.params.id, worker_id]);
    if (dup) return res.status(409).json({ ok: false, error: 'worker já convidado para este job' });

    // Conflito de horário com service_jobs existente
    const [sjConflict] = await _pgQuery(`
      SELECT sj.id, sj.title FROM public.job_assignments ja
      JOIN public.service_jobs sj ON sj.id = ja.job_id
      WHERE ja.worker_id=$1 AND ja.status NOT IN ('cancelled')
        AND sj.status NOT IN ('cancelled','completed')
        AND sj.starts_at < $3 AND sj.ends_at > $2
      LIMIT 1
    `, [worker_id, job.starts_at, job.ends_at]);
    if (sjConflict) return res.status(409).json({
      ok: false, error: 'conflito com serviço confirmado',
      conflict: { type: 'service_job', id: sjConflict.id, title: sjConflict.title },
    });

    // Criar application
    const [row] = await _pgQuery(`
      INSERT INTO public.marketplace_applications (job_id, worker_id, notes)
      VALUES ($1,$2,$3) RETURNING id
    `, [req.params.id, worker_id, notes||null]);

    await _mkAudit('marketplace_invite_sent', parseInt(req.params.id),
      { worker_id, application_id: row.id });
    console.log(JSON.stringify({ event: 'marketplace_invites_sent',
      job_id: req.params.id, worker_id, application_id: row.id }));

    // Enviar WhatsApp se solicitado
    if (send_whatsapp) {
      const { execSync } = require('child_process');
      try {
        execSync(`python3 /home/jdl/ai-os/bin/marketplace_invite.py --job-id ${req.params.id} --worker-id ${worker_id}`,
          { timeout: 15000 });
      } catch(e) { /* log mas não falha a resposta */ }
    }

    res.json({ ok: true, application_id: row.id });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/marketplace/applications/:id/select', requireRole('operator'), async (req, res) => {
  if (!_pgClient) return res.status(500).json({ ok: false, error: 'pg not available' });
  const client = await _pgClient.connect();
  try {
    await client.query('BEGIN');

    // Lock application + join job in one shot
    const appR = await client.query(`
      SELECT ma.id, ma.job_id, ma.worker_id, ma.status,
             mj.title, mj.starts_at, mj.ends_at, mj.client_id,
             mj.needed_workers, mj.location, mj.service_job_id, mj.status AS job_status
      FROM public.marketplace_applications ma
      JOIN public.marketplace_jobs mj ON mj.id = ma.job_id
      WHERE ma.id=$1 FOR UPDATE OF ma
    `, [req.params.id]);
    const app = appR.rows[0];

    if (!app) {
      await client.query('ROLLBACK');
      return res.status(404).json({ ok: false, error: 'not found' });
    }
    if (!['invited','accepted'].includes(app.status)) {
      await client.query('ROLLBACK');
      return res.status(400).json({ ok: false, error: `status é ${app.status}` });
    }
    if (app.job_status === 'matched') {
      await client.query('ROLLBACK');
      return res.status(409).json({ ok: false, error: 'vaga já preenchida' });
    }
    if (['closed','cancelled'].includes(app.job_status)) {
      await client.query('ROLLBACK');
      return res.status(409).json({ ok: false, error: `job ${app.job_status}` });
    }

    // Over-selection guard (recount inside transaction)
    const selR = await client.query(
      `SELECT COUNT(*) AS n FROM public.marketplace_applications WHERE job_id=$1 AND status='selected'`,
      [app.job_id]);
    if (parseInt(selR.rows[0].n) >= parseInt(app.needed_workers)) {
      await client.query('ROLLBACK');
      return res.status(409).json({ ok: false, error: 'vagas já preenchidas' });
    }

    // Lock marketplace_jobs row to prevent concurrent bridge creation
    const mjR = await client.query(
      `SELECT service_job_id FROM public.marketplace_jobs WHERE id=$1 FOR UPDATE`,
      [app.job_id]);
    let serviceJobId = mjR.rows[0]?.service_job_id ? parseInt(mjR.rows[0].service_job_id) : null;

    if (!serviceJobId) {
      const sjR = await client.query(`
        INSERT INTO public.service_jobs (client_id, title, location, starts_at, ends_at, needed_workers, status)
        VALUES ($1,$2,$3,$4,$5,$6,'in_progress') RETURNING id
      `, [app.client_id, app.title, app.location||null, app.starts_at, app.ends_at, app.needed_workers]);
      serviceJobId = sjR.rows[0].id;
      await client.query(`UPDATE public.marketplace_jobs SET service_job_id=$1 WHERE id=$2`,
        [serviceJobId, app.job_id]);
    }

    // Idempotency: skip duplicate assignment
    const dupR = await client.query(
      `SELECT id FROM public.job_assignments WHERE job_id=$1 AND worker_id=$2 AND status!='cancelled' LIMIT 1`,
      [serviceJobId, app.worker_id]);
    let assignmentId;
    if (dupR.rows.length > 0) {
      assignmentId = dupR.rows[0].id;
    } else {
      const asgR = await client.query(`
        INSERT INTO public.job_assignments (job_id, worker_id, status)
        VALUES ($1,$2,'confirmed') RETURNING id
      `, [serviceJobId, app.worker_id]);
      assignmentId = asgR.rows[0].id;
    }

    // Mark selected
    await client.query(
      `UPDATE public.marketplace_applications SET status='selected', response_at=now() WHERE id=$1`,
      [req.params.id]);

    // Promote job to matched if needed_workers reached
    const newSelR = await client.query(
      `SELECT COUNT(*) AS n FROM public.marketplace_applications WHERE job_id=$1 AND status='selected'`,
      [app.job_id]);
    if (parseInt(newSelR.rows[0].n) >= parseInt(app.needed_workers)) {
      await client.query(`UPDATE public.marketplace_jobs SET status='matched' WHERE id=$1`, [app.job_id]);
    }

    await client.query('COMMIT');

    await _mkAudit('marketplace_worker_selected', parseInt(req.params.id),
      { worker_id: app.worker_id, job_id: app.job_id, service_job_id: serviceJobId, assignment_id: assignmentId });
    console.log(JSON.stringify({ event: 'marketplace_worker_selected',
      application_id: req.params.id, worker_id: app.worker_id,
      job_id: app.job_id, service_job_id: serviceJobId, assignment_id: assignmentId }));
    res.json({ ok: true, service_job_id: serviceJobId, assignment_id: assignmentId });
  } catch(e) {
    try { await client.query('ROLLBACK'); } catch(_) {}
    res.status(500).json({ ok: false, error: String(e.message) });
  } finally {
    client.release();
  }
});

app.post('/api/admin/marketplace/applications/:id/reject', requireRole('operator'), async (req, res) => {
  try {
    const [app] = await _pgQuery(
      `SELECT id, job_id, status FROM public.marketplace_applications WHERE id=$1`, [req.params.id]);
    if (!app) return res.status(404).json({ ok: false, error: 'not found' });
    if (['selected','expired'].includes(app.status))
      return res.status(400).json({ ok: false, error: `não pode rejeitar status=${app.status}` });
    await _pgQuery(`UPDATE public.marketplace_applications SET status='rejected', response_at=now() WHERE id=$1`,
      [req.params.id]);
    await _mkAudit('marketplace_worker_rejected', parseInt(req.params.id), { job_id: app.job_id });
    console.log(JSON.stringify({ event: 'marketplace_worker_rejected',
      application_id: req.params.id, job_id: app.job_id }));
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/marketplace/jobs/:id/close', requireRole('operator'), async (req, res) => {
  try {
    const { action = 'close', reason } = req.body;
    if (!['close','cancel'].includes(action))
      return res.status(400).json({ ok: false, error: 'action deve ser close ou cancel' });
    const newStatus = action === 'cancel' ? 'cancelled' : 'closed';
    const [row] = await _pgQuery(
      `UPDATE public.marketplace_jobs SET status=$1
       WHERE id=$2 AND status NOT IN ('closed','cancelled') RETURNING id`,
      [newStatus, req.params.id]);
    if (!row) return res.status(409).json({ ok: false, error: 'job já fechado/cancelado ou não existe' });
    // Expire pending invitations when cancelling
    if (newStatus === 'cancelled') {
      await _pgQuery(
        `UPDATE public.marketplace_applications SET status='expired'
         WHERE job_id=$1 AND status IN ('invited','accepted')`,
        [req.params.id]);
    }
    const evtName = action === 'cancel' ? 'marketplace_job_cancelled' : 'marketplace_job_closed';
    await _mkAudit(evtName, parseInt(req.params.id), { reason: reason || null });
    console.log(JSON.stringify({ event: evtName, job_id: req.params.id, reason }));
    res.json({ ok: true, status: newStatus });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

// ── Admin Clients (lookup) ────────────────────────────────────────────────────

app.get('/api/admin/clients', requireRole('viewer'), async (req, res) => {
  try {
    const rows = await _pgQuery(
      `SELECT id, name FROM public.clients ORDER BY name`, []);
    res.json({ ok: true, clients: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

// ── Admin Planeamento / Escalas ───────────────────────────────────────────────

app.get('/api/admin/jobs', requireRole('operator'), async (req, res) => {
  try {
    const { status, from } = req.query;
    const params = [];
    let where = 'WHERE 1=1';
    if (status) { params.push(status); where += ` AND sj.status=$${params.length}`; }
    if (from)   { params.push(from);   where += ` AND sj.starts_at>=$${params.length}`; }
    const rows = await _pgQuery(`
      SELECT sj.id, sj.title, sj.location, sj.starts_at, sj.ends_at,
             sj.needed_workers, sj.status, sj.notes, sj.created_at,
             c.name AS client_name,
             COUNT(ja.id) FILTER (WHERE ja.status != 'cancelled')           AS assigned_count,
             COUNT(ja.id) FILTER (WHERE ja.status = 'confirmed')            AS confirmed_count,
             COUNT(ja.id) FILTER (WHERE ja.status IN ('confirmed','checked_in','completed')) AS ready_count
      FROM public.service_jobs sj
      LEFT JOIN public.clients c ON c.id = sj.client_id
      LEFT JOIN public.job_assignments ja ON ja.job_id = sj.id
      ${where}
      GROUP BY sj.id, c.name
      ORDER BY sj.starts_at ASC LIMIT 200
    `, params);
    res.json({ ok: true, jobs: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/jobs', requireRole('operator'), async (req, res) => {
  try {
    const { client_id, title, location, starts_at, ends_at, needed_workers = 1, notes } = req.body;
    if (!client_id || !title || !starts_at || !ends_at)
      return res.status(400).json({ ok: false, error: 'client_id, title, starts_at, ends_at obrigatórios' });
    if (new Date(ends_at) <= new Date(starts_at))
      return res.status(400).json({ ok: false, error: 'ends_at deve ser posterior a starts_at' });
    const [row] = await _pgQuery(`
      INSERT INTO public.service_jobs (client_id, title, location, starts_at, ends_at, needed_workers, notes)
      VALUES ($1,$2,$3,$4,$5,$6,$7)
      RETURNING id
    `, [client_id, title, location || null, starts_at, ends_at, needed_workers, notes || null]);
    console.log(JSON.stringify({ event: 'service_job_created', job_id: row.id, title, client_id, user: req.user?.sub }));
    res.json({ ok: true, job_id: row.id });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.get('/api/admin/jobs/:id', requireRole('operator'), async (req, res) => {
  try {
    const [job, assignments] = await Promise.all([
      _pgQuery(`
        SELECT sj.*, c.name AS client_name
        FROM public.service_jobs sj
        LEFT JOIN public.clients c ON c.id = sj.client_id
        WHERE sj.id=$1
      `, [req.params.id]),
      _pgQuery(`
        SELECT ja.*, p.name AS worker_name
        FROM public.job_assignments ja
        JOIN public.persons p ON p.id = ja.worker_id
        WHERE ja.job_id=$1
        ORDER BY ja.assigned_at
      `, [req.params.id]),
    ]);
    if (!job.length) return res.status(404).json({ ok: false, error: 'not found' });
    res.json({ ok: true, job: job[0], assignments });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/jobs/:id/assign', requireRole('operator'), async (req, res) => {
  try {
    const { worker_id, role, notes } = req.body;
    if (!worker_id) return res.status(400).json({ ok: false, error: 'worker_id obrigatório' });
    const [job] = await _pgQuery(
      `SELECT id, starts_at, ends_at, status FROM public.service_jobs WHERE id=$1`, [req.params.id]);
    if (!job) return res.status(404).json({ ok: false, error: 'job not found' });
    if (job.status === 'cancelled') return res.status(400).json({ ok: false, error: 'job cancelado' });

    // Conflict check: worker already assigned to overlapping job
    const [conflict] = await _pgQuery(`
      SELECT sj.id AS conflict_job_id, sj.title AS conflict_title,
             sj.starts_at, sj.ends_at
      FROM public.job_assignments ja
      JOIN public.service_jobs sj ON sj.id = ja.job_id
      WHERE ja.worker_id = $1
        AND ja.status NOT IN ('cancelled')
        AND ja.job_id != $2
        AND sj.status NOT IN ('cancelled','completed')
        AND sj.starts_at < $4
        AND sj.ends_at   > $3
      LIMIT 1
    `, [worker_id, req.params.id, job.starts_at, job.ends_at]);

    if (conflict) {
      console.log(JSON.stringify({
        event: 'worker_assignment_conflict', job_id: req.params.id, worker_id,
        conflict_job_id: conflict.conflict_job_id,
      }));
      return res.status(409).json({
        ok: false, error: 'conflito de horário',
        conflict: { job_id: conflict.conflict_job_id, title: conflict.conflict_title,
                    starts_at: conflict.starts_at, ends_at: conflict.ends_at },
      });
    }

    const [row] = await _pgQuery(`
      INSERT INTO public.job_assignments (job_id, worker_id, role, notes)
      VALUES ($1,$2,$3,$4) RETURNING id
    `, [req.params.id, worker_id, role || null, notes || null]);
    console.log(JSON.stringify({ event: 'worker_assigned', job_id: req.params.id, worker_id, assignment_id: row.id }));
    res.json({ ok: true, assignment_id: row.id });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/assignments/:id/confirm', requireRole('operator'), async (req, res) => {
  try {
    const [asg] = await _pgQuery(
      `SELECT id, job_id, status FROM public.job_assignments WHERE id=$1`, [req.params.id]);
    if (!asg) return res.status(404).json({ ok: false, error: 'not found' });
    if (asg.status !== 'assigned') return res.status(400).json({ ok: false, error: `status é ${asg.status}` });
    await _pgQuery(`
      UPDATE public.job_assignments SET status='confirmed', confirmed_at=now() WHERE id=$1
    `, [req.params.id]);
    // Promover job para 'ready' se confirmados >= needed_workers
    await _pgQuery(`
      UPDATE public.service_jobs SET status='ready'
      WHERE id=$1 AND status='planned'
        AND (SELECT COUNT(*) FROM public.job_assignments
             WHERE job_id=$1 AND status='confirmed') >= needed_workers
    `, [asg.job_id]);
    console.log(JSON.stringify({ event: 'worker_assignment_confirmed', assignment_id: req.params.id, job_id: asg.job_id }));
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

app.post('/api/admin/assignments/:id/cancel', requireRole('operator'), async (req, res) => {
  try {
    const [asg] = await _pgQuery(
      `SELECT id, job_id FROM public.job_assignments WHERE id=$1`, [req.params.id]);
    if (!asg) return res.status(404).json({ ok: false, error: 'not found' });
    await _pgQuery(`
      UPDATE public.job_assignments SET status='cancelled' WHERE id=$1
    `, [req.params.id]);
    // Reverter job para 'planned' se já não tem confirmados suficientes
    await _pgQuery(`
      UPDATE public.service_jobs SET status='planned'
      WHERE id=$1 AND status='ready'
        AND (SELECT COUNT(*) FROM public.job_assignments
             WHERE job_id=$1 AND status='confirmed') < needed_workers
    `, [asg.job_id]);
    res.json({ ok: true });
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message) }); }
});

// All endpoints: requireClientAccess → filters by req.user.client_id (or all if admin)

function _clientId(req) {
  const lvl = ROLE_LEVEL[req.user?.role] || 0;
  if (lvl >= 80) return null; // admin: no filter
  return req.user?.client_id || null;
}

const _pgClient = (() => {
  const { Pool } = (() => { try { return require('pg'); } catch(e) { return null; } })() || {};
  if (!Pool) return null;
  return new Pool({ connectionString: process.env.DATABASE_URL || 'postgresql://aios_user:jdl@127.0.0.1/aios' });
})();

async function _pgQuery(sql, params) {
  if (!_pgClient) throw new Error('pg module not available');
  const r = await _pgClient.query(sql, params);
  return r.rows;
}

async function _mkAudit(kind, entity_id, data) {
  try {
    await _pgQuery(
      `INSERT INTO public.events (source, kind, entity_id, message, data, level)
       VALUES ('marketplace', $1, $2, $3, $4::jsonb, 'info')`,
      [kind, entity_id || null, kind.replace(/_/g, ' '), JSON.stringify(data || {})]
    );
  } catch(_) { /* audit must never break main flow */ }
}

// Overview
app.get('/api/client/overview', requireClientAccess, async (req, res) => {
  try {
    const cid = _clientId(req);
    const filter = cid ? 'AND et.client_id=$1' : '';
    const p = cid ? [cid] : [];
    const [active, pending, completed, expenses, monthly] = await Promise.all([
      _pgQuery(`SELECT COUNT(*) AS n FROM public.event_timesheets et
                WHERE et.status='submitted' AND et.log_date=CURRENT_DATE ${filter}`, p),
      _pgQuery(`SELECT COUNT(*) AS n FROM public.event_timesheets et
                WHERE et.status='submitted' ${filter}`, p),
      _pgQuery(`SELECT COUNT(*) AS n, COALESCE(SUM(
                  COALESCE(et.adjusted_invoice_total,et.invoice_total)
                ),0) AS total
                FROM public.event_timesheets et
                WHERE et.status IN ('approved_client','adjusted_client','validated','invoiced_mock') ${filter}`, p),
      _pgQuery(`SELECT COUNT(*) FILTER (WHERE te.status='pending_client_review') AS pending,
                       COUNT(*) FILTER (WHERE te.status='approved_client') AS approved,
                       COALESCE(SUM(te.amount) FILTER (WHERE te.status='approved_client'),0) AS approved_amount
                FROM public.timesheet_expenses te
                JOIN public.event_timesheets et ON et.id=te.timesheet_id
                WHERE 1=1 ${filter}`, p),
      _pgQuery(`SELECT COALESCE(SUM(COALESCE(et.adjusted_invoice_total,et.invoice_total)),0) AS total
                FROM public.event_timesheets et
                WHERE et.status IN ('approved_client','adjusted_client','validated','invoiced_mock')
                AND et.log_date >= date_trunc('month',CURRENT_DATE) ${filter}`, p),
    ]);
    res.json({
      ok: true,
      active_today:       parseInt(active[0]?.n || 0),
      pending_validation: parseInt(pending[0]?.n || 0),
      completed:          parseInt(completed[0]?.n || 0),
      invoiced_total:     parseFloat(completed[0]?.total || 0),
      expenses_pending:   parseInt(expenses[0]?.pending || 0),
      expenses_approved:  parseInt(expenses[0]?.approved || 0),
      expenses_approved_amount: parseFloat(expenses[0]?.approved_amount || 0),
      monthly_total:      parseFloat(monthly[0]?.total || 0),
    });
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Timesheets (no worker_pay, no payout internal data)
app.get('/api/client/timesheets', requireClientAccess, async (req, res) => {
  try {
    const cid = _clientId(req);
    const limit = Math.min(parseInt(req.query.limit) || 50, 200);
    const status = req.query.status || '';
    const params = cid ? [cid] : [];
    let statusClause = '';
    if (status) {
      params.push(status);
      statusClause = `AND et.status=$${params.length}`;
    }
    const cidClause = cid ? 'AND et.client_id=$1' : '';
    params.push(limit);
    const rows = await _pgQuery(`
      SELECT et.id, et.worker_id, et.log_date, et.hours,
             et.days_equivalent, et.location, et.status,
             et.start_time, et.check_out_at,
             et.notes, et.invoice_net, et.invoice_vat, et.invoice_total,
             et.adjusted_days, et.adjusted_invoice_total,
             COALESCE(et.adjusted_invoice_total, et.invoice_total) AS effective_total,
             et.validation_token, et.validated_at, et.validator_note,
             et.client_extras
      FROM public.event_timesheets et
      WHERE (et.validation_token IS NOT NULL OR et.source='manual')
        ${cidClause} ${statusClause}
      ORDER BY et.log_date DESC, et.id DESC
      LIMIT $${params.length}
    `, params);
    // Serialise dates
    const result = rows.map(r => {
      const d = {...r};
      for (const k of Object.keys(d)) {
        if (d[k] instanceof Date) d[k] = d[k].toISOString();
      }
      return d;
    });
    res.json({ ok: true, timesheets: result });
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Expenses (client view — includes photo path for download)
app.get('/api/client/expenses', requireClientAccess, async (req, res) => {
  try {
    const cid = _clientId(req);
    const params = cid ? [cid] : [];
    const cidClause = cid ? 'AND et.client_id=$1' : '';
    const rows = await _pgQuery(`
      SELECT te.id, te.timesheet_id, te.worker_name,
             te.amount, te.expense_type, te.notes,
             te.receipt_name, te.receipt_nif_name,
             te.receipt_image_url,
             te.status, te.approved_at, te.rejected_reason, te.reimbursed_at,
             te.created_at,
             et.log_date, et.worker_id
      FROM public.timesheet_expenses te
      JOIN public.event_timesheets et ON et.id=te.timesheet_id
      WHERE 1=1 ${cidClause}
      ORDER BY te.created_at DESC
    `, params);
    const result = rows.map(r => {
      const d = {...r};
      for (const k of Object.keys(d)) {
        if (d[k] instanceof Date) d[k] = d[k].toISOString();
      }
      return d;
    });
    res.json({ ok: true, expenses: result });
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Invoices (client view)
app.get('/api/client/invoices', requireClientAccess, async (req, res) => {
  try {
    const cid = _clientId(req);
    const params = cid ? [cid] : [];
    const cidClause = cid ? 'AND ci.client_id=$1' : '';
    const rows = await _pgQuery(`
      SELECT ci.id, ci.invoice_number, ci.status,
             ci.subtotal, ci.tax_total, ci.total,
             ci.issue_date, ci.due_date, ci.paid_at, ci.created_at,
             et.worker_id, et.log_date, et.hours
      FROM public.client_invoices ci
      LEFT JOIN public.event_timesheets et ON et.id = ci.timesheet_id
      ${cidClause ? 'WHERE ' + cidClause.slice(4) : ''}
      ORDER BY ci.created_at DESC LIMIT 100
    `, params);
    // Totais por estado (sem mostrar worker_pay)
    const totals = { draft: 0, invoiced: 0, paid: 0, pending: 0 };
    for (const r of rows) {
      const v = parseFloat(r.total || 0);
      if (r.status === 'paid')           totals.paid     += v;
      else if (r.status === 'invoice_draft') totals.draft += v;
      else if (r.status === 'invoiced')  { totals.invoiced += v; totals.pending += v; }
    }
    res.json({ ok: true, invoices: rows, totals });
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// Monthly breakdown
app.get('/api/client/monthly', requireClientAccess, async (req, res) => {
  try {
    const cid = _clientId(req);
    const params = cid ? [cid] : [];
    const cidClause = cid ? 'AND et.client_id=$1' : '';
    const rows = await _pgQuery(`
      SELECT to_char(et.log_date,'YYYY-MM') AS month,
             COUNT(*) AS services,
             COALESCE(SUM(COALESCE(et.adjusted_invoice_total,et.invoice_total)),0) AS invoice_total,
             COALESCE(SUM(te.amount) FILTER (WHERE te.status='approved_client'),0) AS expenses_total
      FROM public.event_timesheets et
      LEFT JOIN public.timesheet_expenses te ON te.timesheet_id=et.id
      WHERE et.status IN ('approved_client','adjusted_client','validated','invoiced_mock')
        ${cidClause}
      GROUP BY 1 ORDER BY 1 DESC
      LIMIT 12
    `, params);
    res.json({ ok: true, monthly: rows });
  } catch(e) { res.status(500).json({ ok: false, error: String(e) }); }
});

// ── RH Module ──────────────────────────────────────────────────────────────────

const _hrDocsDir = path.join('/home/jdl/ai-os/runtime/hr_docs');
if (!fs.existsSync(_hrDocsDir)) fs.mkdirSync(_hrDocsDir, { recursive: true });

const _hrUpload = multer ? multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => cb(null, _hrDocsDir),
    filename: (req, file, cb) => {
      const ext = path.extname(file.originalname) || '.pdf';
      cb(null, `${Date.now()}_${file.originalname.replace(/[^a-zA-Z0-9._-]/g, '_')}`);
    },
  }),
  limits: { fileSize: 10 * 1024 * 1024 },
  fileFilter: (req, file, cb) => cb(null, /pdf|image\//i.test(file.mimetype) || file.originalname.endsWith('.pdf')),
}) : null;

const _rhExec = (args) => {
  const { execFileSync } = require('child_process');
  const out = execFileSync('python3', ['/home/jdl/ai-os/bin/rh_engine.py', ...args],
    { timeout: 12000, encoding: 'utf8' });
  return JSON.parse(out);
};

app.get('/api/rh/persons', requireRole('operator'), (req, res) => {
  try { res.json(_rhExec(['list_persons'])); }
  catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.get('/api/rh/persons/:id', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    res.json(_rhExec(['get_person', String(id)]));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.put('/api/rh/persons/:id/extra', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    const b = req.body || {};
    const args = ['upsert_extra', String(id)];
    if (b.iban)                args.push('--iban',                 String(b.iban).slice(0,50));
    if (b.niss)                args.push('--niss',                 String(b.niss).slice(0,20));
    if (b.data_nascimento)     args.push('--data-nascimento',      String(b.data_nascimento).slice(0,10));
    if (b.morada)              args.push('--morada',               String(b.morada).slice(0,500));
    if (b.data_admissao)       args.push('--data-admissao',        String(b.data_admissao).slice(0,10));
    if (b.tipo_contrato_atual) args.push('--tipo-contrato-atual',  String(b.tipo_contrato_atual).slice(0,30));
    res.json(_rhExec(args));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.get('/api/rh/persons/:id/contracts', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    res.json(_rhExec(['list_contracts', String(id)]));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.post('/api/rh/persons/:id/contracts', requireRole('operator'),
  ...(_hrUpload ? [_hrUpload.single('file')] : [(req, res, next) => next()]),
  (req, res) => {
    try {
      const personId = parseInt(req.params.id);
      if (!personId) return res.status(400).json({ error: 'id inválido' });
      const b = req.body || {};
      if (!b.tipo || !b.data_inicio)
        return res.status(400).json({ ok: false, error: 'tipo e data_inicio obrigatórios' });
      const args = ['add_contract', String(personId),
        '--tipo',        String(b.tipo).slice(0,30),
        '--data-inicio', String(b.data_inicio).slice(0,10)];
      if (b.data_fim) args.push('--data-fim', String(b.data_fim).slice(0,10));
      if (b.notas)    args.push('--notas',    String(b.notas).slice(0,500));
      if (req.file)   args.push('--file-path', req.file.filename);
      res.json(_rhExec(args));
    } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
  }
);

app.delete('/api/rh/contracts/:id', requireRole('admin'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    res.json(_rhExec(['delete_contract', String(id)]));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.get('/api/rh/persons/:id/documents', requireRole('operator'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    res.json(_rhExec(['list_documents', String(id)]));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.post('/api/rh/persons/:id/documents', requireRole('operator'),
  ...(_hrUpload ? [_hrUpload.single('file')] : [(req, res, next) => next()]),
  (req, res) => {
    try {
      const personId = parseInt(req.params.id);
      if (!personId) return res.status(400).json({ error: 'id inválido' });
      if (!req.file) return res.status(400).json({ ok: false, error: 'ficheiro obrigatório' });
      const b = req.body || {};
      if (!b.tipo) return res.status(400).json({ ok: false, error: 'tipo obrigatório' });
      const args = ['add_document', String(personId),
        '--tipo',      String(b.tipo).slice(0,20),
        '--file-path', req.file.filename];
      if (b.descricao) args.push('--descricao', String(b.descricao).slice(0,200));
      res.json(_rhExec(args));
    } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
  }
);

app.delete('/api/rh/documents/:id', requireRole('admin'), (req, res) => {
  try {
    const id = parseInt(req.params.id);
    if (!id) return res.status(400).json({ error: 'id inválido' });
    res.json(_rhExec(['delete_document', String(id)]));
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.get('/api/rh/files/:filename', requireRole('operator'), (req, res) => {
  try {
    const fn = path.basename(req.params.filename); // strip traversal
    const fp = path.join(_hrDocsDir, fn);
    if (!fs.existsSync(fp)) return res.status(404).json({ error: 'ficheiro não encontrado' });
    res.setHeader('Content-Disposition', `inline; filename="${fn}"`);
    res.sendFile(fp);
  } catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,200) }); }
});

app.get('/api/rh/stats', requireRole('operator'), (req, res) => {
  try { res.json(_rhExec(['get_stats'])); }
  catch(e) { res.status(500).json({ ok: false, error: String(e.message||e).slice(0,300) }); }
});

app.listen(3000, () => console.log("UI http://localhost:3000"));
