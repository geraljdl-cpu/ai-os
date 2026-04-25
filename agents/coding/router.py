"""
agents/coding/router.py — Central model routing for the coding subsystem.

Single source of truth for model selection.
All other modules call get_model(task_type) — never hard-code model names elsewhere.
Config is loaded from config/local_coding_agent.yaml.
"""
import os
import pathlib
import yaml

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
CONFIG_PATH = AIOS_ROOT / "config" / "local_coding_agent.yaml"

_config_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    if _config_cache is None:
        if not CONFIG_PATH.exists():
            raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
        with open(CONFIG_PATH) as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def reload_config() -> dict:
    global _config_cache
    _config_cache = None
    return load_config()


def get_ollama_endpoint() -> str:
    cfg = load_config()
    return cfg["ollama"]["endpoint"]


def get_model(task_type: str) -> str:
    """
    Return the model name for a given task type.

    task_type values: "coding", "reasoning", "light", "review", "plan"
    Falls back to "coding" model for unknown types.
    """
    cfg = load_config()
    models = cfg.get("models", {})
    mapping = {
        "coding":    models.get("coding",    "qwen2.5-coder:14b"),
        "reasoning": models.get("reasoning", "deepseek-r1:14b"),
        "light":     models.get("light",     "mistral:7b"),
        "review":    models.get("review",    models.get("coding", "qwen2.5-coder:14b")),
        "plan":      models.get("plan",      models.get("coding", "qwen2.5-coder:14b")),
        "debug":     models.get("debug",     models.get("reasoning", "deepseek-r1:14b")),
    }
    return mapping.get(task_type.lower(), mapping["coding"])


def get_routing_info() -> dict:
    """Return full routing table for inspection/debugging."""
    cfg = load_config()
    return {
        "endpoint": get_ollama_endpoint(),
        "models": cfg.get("models", {}),
        "routing": {t: get_model(t) for t in ("coding", "reasoning", "light", "review", "plan", "debug")},
    }


def get_cloud_fallback_config() -> dict | None:
    """
    Return cloud fallback config dict if enabled in local_coding_agent.yaml, else None.

    Local-first is always the default. This only returns a value when the operator
    explicitly sets cloud_fallback.enabled=true. Callers should treat None as
    "fallback not available — let the error propagate."
    """
    cfg = load_config()
    fb = cfg.get("cloud_fallback", {})
    if not fb.get("enabled", False):
        return None
    return {
        "provider": fb.get("provider", "openai"),
        "model":    fb.get("model", "gpt-4o-mini"),
        "trigger":  fb.get("trigger", "ollama_unreachable"),
    }


def cloud_generate(prompt: str, model: str) -> str:
    """
    Cloud LLM completion via OpenAI-compatible API (fallback only).

    Only called when:
      - cloud_fallback.enabled=true in config/local_coding_agent.yaml
      - Ollama is unreachable (RuntimeError from _ollama_generate)

    Requires: OPENAI_API_KEY env var + openai package installed.
    """
    try:
        import openai
    except ImportError as exc:
        raise RuntimeError(
            "openai package not installed; run: pip install openai"
        ) from exc
    client = openai.OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=1024,
    )
    return resp.choices[0].message.content.strip()


if __name__ == "__main__":
    import json
    print(json.dumps(get_routing_info(), indent=2))
