const express = require("express");
const path = require('path');
const fs = require("fs");
const { exec } = require("child_process");
const app = express();
app.use(express.static(path.join(__dirname, 'public')));


// --- UI -> Agent API proxy ---
app.use(express.json({ limit: "1mb" }));


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

app.use(express.json());


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
  try{ res.json(JSON.parse(fs.readFileSync(BACKLOG))); }
  catch{ res.json({tasks:[]}); }
});

app.post("/api/add",(req,res)=>{
  const {goal}=req.body;
  const data=JSON.parse(fs.readFileSync(BACKLOG));
  data.tasks.push({id:Date.now().toString(),goal});
  fs.writeFileSync(BACKLOG,JSON.stringify(data,null,2));
  res.json({ok:true});
});

app.post("/api/run",(req,res)=>{
  exec(HOME + "/ai-os/bin/autopilot_worker.sh");
  res.json({ok:true});
});

app.get("/api/status",(req,res)=>{
  try{
    const backlog=JSON.parse(fs.readFileSync(BACKLOG));
    const running = backlog.tasks.length > 0 ? "WORKING" : "IDLE";
    res.json({status:running});
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



app.listen(3000,()=>console.log("UI http://localhost:3000"));

// últimos logs do agent-router
app.get("/api/logs",(req,res)=>{
  const { exec } = require("child_process");
  exec("docker logs agent-router --tail 100", (err,stdout,stderr)=>{
    res.json({logs: stdout || stderr});
  });
});

// Finance routes — Toconline
const { execSync } = require('child_process');
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
