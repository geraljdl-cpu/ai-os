const express = require("express");
const app = express();
app.use(express.json());

// CORS restrito ao UI
app.use((req,res,next)=>{
  const o = req.headers.origin || "";
  if (o === "http://localhost:3000" || o === "http://127.0.0.1:3000") {
    res.setHeader("Access-Control-Allow-Origin", o);
    res.setHeader("Vary", "Origin");
  }
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  res.setHeader("Access-Control-Allow-Methods", "POST,GET,OPTIONS");
  if (req.method === "OPTIONS") return res.sendStatus(200);
  next();
});

app.get("/health", (req,res)=>res.json({ok:true}));

app.post("/api/exec", async (req,res)=>{
  try{
    const args = req.body && req.body.args;
    if(!Array.isArray(args) || !args.length) return res.status(400).json({ok:false,error:"args required"});
    const r = await fetch("http://127.0.0.1:8020/run", {
      method:"POST",
      headers:{ "Content-Type":"application/json" },
      body: JSON.stringify({ args })
    });
    const data = await r.json().catch(()=>({}));
    return res.status(r.status).json(data);
  }catch(e){
    return res.status(500).json({ok:false,error:String(e)});
  }
});

app.listen(3001, "127.0.0.1", ()=>console.log("exec-proxy http://127.0.0.1:3001"));
