const $ = (id) => document.getElementById(id);

function setRunning(on) {
  $("run").disabled = on;
  $("run").textContent = on ? "RUNNING..." : "RUN";
}

function pretty(x) {
  if (typeof x === "string") return x;
  try { return JSON.stringify(x, null, 2); } catch { return String(x); }
}

async function run() {
  const mode = $("mode").value || "openai";
  const quick = $("quick").value.trim();
  const cmd = $("cmd").value.trim();
  const chatInput = cmd || quick;

  if (!chatInput) {
    $("out").textContent = "Escreve algo em chatInput ou Quick.";
    return;
  }

  setRunning(true);
  $("out").textContent = "A enviar para /api/agent ...";

  try {
    const r = await fetch("/api/agent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode, chatInput }),
    });

    const j = await r.json().catch(async () => ({ raw: await r.text() }));
    if (!r.ok) {
      $("out").textContent = "ERRO HTTP " + r.status + "\n\n" + pretty(j);
      return;
    }

    // tenta mostrar o "answer" do upstream (quando existir)
    const data = j?.data;
    const answer = data?.answer ?? data?.steps ?? data ?? j;
    $("out").textContent = pretty(answer);
  } catch (e) {
    $("out").textContent = "ERRO: " + String(e);
  } finally {
    setRunning(false);
  }
}

$("run").addEventListener("click", run);
$("quick").addEventListener("keydown", (e) => { if (e.key === "Enter") run(); });
