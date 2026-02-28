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

// --- Auth middleware (aplica a todas as rotas /api/* excepto /api/auth/login) ---
const AUTH_EXEMPT = new Set(['/auth/login']);
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

app.listen(3000,()=>console.log("UI http://localhost:3000"));
