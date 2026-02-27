/* JDL_EXEC_PROXY_3001 */
(function(){
  function splitArgs(s){ return s.trim().split(" ").filter(Boolean); }
  function proxyUrl(){ return `http://${location.hostname}:3001/api/exec`; }

  function print(txt){
    const el = document.querySelector("#jdlMiniChatMessages") || document.querySelector(".messages") || document.body;
    const d = document.createElement("div");
    d.style.whiteSpace = "pre-wrap";
    d.textContent = txt;
    el.appendChild(d);
    if (el.scrollTop !== undefined) el.scrollTop = el.scrollHeight;
  }

  async function handleIfBang(){
    const input =
      document.querySelector('textarea[placeholder*="Escreve"]') ||
      document.querySelector('input[placeholder*="Escreve"]') ||
      document.querySelector('textarea') ||
      document.querySelector('input[type="text"]');

    if(!input) return false;

    const v = (input.value||"").trim();
    if(!v.startsWith("!")) return false;

    const cmdLine = v.slice(1).trim();
    input.value = "";
    print("$ " + cmdLine);

    try{
      const r = await fetch(proxyUrl(), {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ args: splitArgs(cmdLine) })
      });
      const data = await r.json().catch(()=>({}));
      if(r.status!==200 || !data || data.ok!==true){
        print((data && (data.error || JSON.stringify(data))) || ("ERR " + r.status));
        return true;
      }
      if (data.stdout) print(data.stdout);
      if (data.stderr) print(data.stderr);
      if (!data.stdout && !data.stderr) print("(no output)");
      return true;
    }catch(err){
      print("ERR " + String(err));
      return true;
    }
  }

  // 1) Interceptar clique no botão SEND
  function hookSendButton(){
    const btn = Array.from(document.querySelectorAll("button")).find(b => (b.textContent||"").trim().toUpperCase()==="SEND");
    if(!btn) return false;
    if(btn.__jdl_hooked) return true;
    btn.__jdl_hooked = true;
    btn.addEventListener("click", async (e)=>{
      const did = await handleIfBang();
      if(did){ e.stopImmediatePropagation(); e.preventDefault(); }
    }, true);
    return true;
  }

  // 2) Interceptar Enter no input (backup)
  window.addEventListener("keydown", async (e)=>{
    if(e.key!=="Enter" || e.shiftKey) return;
    const did = await handleIfBang();
    if(did){ e.preventDefault(); e.stopImmediatePropagation(); }
  }, true);

  // tentar hook agora e depois em loop (porque UI monta tarde)
  hookSendButton();
  setInterval(hookSendButton, 800);
})();
