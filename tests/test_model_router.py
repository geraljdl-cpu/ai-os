#!/usr/bin/env python3
"""
Testes para bin/model_router.py e bin/provider_health.py

Execução:
  python3 tests/test_model_router.py
  python3 -m pytest tests/test_model_router.py -v   (se pytest instalado)
"""
import sys, os, json, pathlib, unittest

# Garante que bin/ está no path
AIOS_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(AIOS_ROOT / "bin"))

import model_router as mr


class TestHeuristic(unittest.TestCase):
    """Testes da heurística de routing sem dependências externas."""

    def _decide(self, job, claude_ok=True, ollama_ok=True, force=""):
        os.environ["HYBRID_MODE"]  = "true"
        os.environ["FORCE_MODEL"]  = force
        os.environ["OLLAMA_MODEL"] = "qwen2.5:14b"
        os.environ["CLAUDE_MODEL"] = "claude-sonnet-4-6"
        # Limpa override de runtime para testes não interferirem
        override = AIOS_ROOT / "runtime" / "model_override.json"
        if override.exists():
            override.write_text('{"force_model":""}')
        return mr.decide_model(
            job=job,
            system_state={"claude_available": claude_ok, "ollama_available": ollama_ok}
        )

    # ── 1. Job simples → Ollama ────────────────────────────────────────────────
    def test_simple_job_goes_to_ollama(self):
        job    = {"task_type": "OPS", "goal": "verificar status do servidor"}
        result = self._decide(job)
        self.assertEqual(result["provider"], "ollama",
                         f"Job simples devia ir para Ollama, foi para {result['provider']} ({result['reason']})")

    def test_monitoring_keyword_goes_to_ollama(self):
        job    = {"goal": "monitoring temperatura fábrica"}
        result = self._decide(job)
        self.assertEqual(result["provider"], "ollama")

    # ── 2. Job complexo → Claude ───────────────────────────────────────────────
    def test_dev_task_goes_to_claude(self):
        job    = {"task_type": "DEV_TASK", "goal": "implementar módulo de autenticação"}
        result = self._decide(job)
        self.assertEqual(result["provider"], "claude",
                         f"DEV_TASK devia ir para Claude, foi para {result['provider']}")

    def test_high_priority_goes_to_claude(self):
        job    = {"goal": "tarefa qualquer", "priority": 9}
        result = self._decide(job)
        self.assertEqual(result["provider"], "claude")

    def test_refactor_keyword_goes_to_claude(self):
        job    = {"goal": "refactor do módulo de backlog"}
        result = self._decide(job)
        self.assertEqual(result["provider"], "claude")

    # ── 3. Claude indisponível → fallback Ollama ───────────────────────────────
    def test_claude_unavailable_fallback_to_ollama(self):
        job    = {"task_type": "DEV_TASK", "goal": "implementar feature X"}
        result = self._decide(job, claude_ok=False, ollama_ok=True)
        self.assertEqual(result["provider"], "ollama",
                         "Com Claude indisponível, devia fazer fallback para Ollama")
        self.assertIn("fallback", result.get("reason", "").lower(),
                      "Reason devia indicar fallback")
        self.assertEqual(result.get("original_provider"), "claude")

    # ── 4. FORCE_MODEL=ollama → sempre Ollama ─────────────────────────────────
    def test_force_ollama(self):
        job    = {"task_type": "DEV_TASK", "goal": "arquitectura complexa"}
        result = self._decide(job, force="ollama")
        self.assertEqual(result["provider"], "ollama")
        self.assertFalse(result.get("fallback_allowed"),
                         "FORCE_MODEL não deve permitir fallback")
        self.assertIn("FORCE_MODEL=ollama", result["reason"])

    # ── 5. FORCE_MODEL=claude → Claude se disponível ──────────────────────────
    def test_force_claude(self):
        job    = {"task_type": "OPS", "goal": "status simples"}
        result = self._decide(job, force="claude")
        self.assertEqual(result["provider"], "claude")
        self.assertIn("FORCE_MODEL=claude", result["reason"])

    # ── 6. Sem config Anthropic (claude_ok=False) → Ollama ────────────────────
    def test_no_anthropic_config_goes_to_ollama(self):
        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            job    = {"task_type": "DEV_TASK", "goal": "gerar código"}
            result = self._decide(job, claude_ok=False)
            self.assertEqual(result["provider"], "ollama")
        finally:
            if old: os.environ["ANTHROPIC_API_KEY"] = old

    # ── 7. Todos os providers indisponíveis → erro explícito ──────────────────
    def test_all_providers_unavailable_returns_none(self):
        job    = {"goal": "qualquer coisa"}
        result = self._decide(job, claude_ok=False, ollama_ok=False)
        self.assertIsNone(result["provider"])
        self.assertIn("unavailable", result["reason"])
        self.assertIn("error", result)

    # ── 8. HYBRID_MODE=false → Claude fixo sem fallback ───────────────────────
    def test_hybrid_mode_false(self):
        os.environ["HYBRID_MODE"]  = "false"
        os.environ["FORCE_MODEL"]  = ""
        os.environ["CLAUDE_MODEL"] = "claude-sonnet-4-6"
        override = AIOS_ROOT / "runtime" / "model_override.json"
        if override.exists(): override.write_text('{"force_model":""}')
        result = mr.decide_model(
            job={"goal": "qualquer"},
            system_state={"claude_available": True, "ollama_available": True}
        )
        self.assertEqual(result["provider"], "claude")
        self.assertFalse(result.get("fallback_allowed"))
        os.environ["HYBRID_MODE"] = "true"


class TestRuntimeOverride(unittest.TestCase):
    """Testa o mecanismo de override de runtime (sem I/O real de rede)."""

    def setUp(self):
        self.override = AIOS_ROOT / "runtime" / "model_override.json"

    def tearDown(self):
        if self.override.exists():
            self.override.write_text('{"force_model":""}')

    def test_set_and_get_override(self):
        mr.set_runtime_override("ollama")
        val = mr.get_runtime_override()
        self.assertEqual(val, "ollama")

    def test_clear_override(self):
        mr.set_runtime_override("claude")
        mr.set_runtime_override("")
        val = mr.get_runtime_override()
        self.assertEqual(val, "")

    def test_runtime_override_takes_effect_in_decide(self):
        mr.set_runtime_override("ollama")
        os.environ["FORCE_MODEL"] = ""  # env vazia, só runtime
        result = mr.decide_model(
            job={"task_type": "DEV_TASK"},
            system_state={"claude_available": True, "ollama_available": True}
        )
        self.assertEqual(result["provider"], "ollama")
        self.assertIn("FORCE_MODEL=ollama", result["reason"])


class TestCreditMonitorBasic(unittest.TestCase):
    """Testes básicos do credit_monitor sem chamadas de rede."""

    def test_record_and_load(self):
        import credit_monitor as cm
        # Usa ficheiro temporário para não poluir runtime real
        orig = cm.USAGE_FILE
        tmp  = AIOS_ROOT / "runtime" / "credit_usage_test.json"
        cm.USAGE_FILE = tmp
        try:
            if tmp.exists(): tmp.unlink()
            u = cm.record_usage(1000, 500, "claude-sonnet-4-6")
            self.assertGreater(u["tokens_used"], 0)
            self.assertGreater(u["estimated_cost_usd"], 0)
        finally:
            cm.USAGE_FILE = orig
            if tmp.exists(): tmp.unlink()

    def test_check_credits_no_threshold_breach(self):
        import credit_monitor as cm
        os.environ["ENABLE_CREDIT_MONITOR"] = "true"
        os.environ["MIN_CREDITS_THRESHOLD"] = "999.0"  # threshold alto → sem alerta
        result = cm.check_credits()
        self.assertTrue(result.get("ok"))
        self.assertFalse(result.get("alert_triggered", False))

    def test_check_credits_disabled(self):
        import credit_monitor as cm
        os.environ["ENABLE_CREDIT_MONITOR"] = "false"
        result = cm.check_credits()
        self.assertTrue(result.get("skipped"))


if __name__ == "__main__":
    # Corre os testes e mostra sumário
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(__import__("__main__"))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
