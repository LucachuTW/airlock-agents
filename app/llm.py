"""Hardware-adaptive local model selection. All inference goes through Ollama."""

import functools
import subprocess
from pathlib import Path

import yaml
from langchain_ollama import ChatOllama, OllamaEmbeddings

from app.config import settings

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "models.yaml"


def _load_config() -> dict:
    return yaml.safe_load(_CONFIG_PATH.read_text())


def detect_vram_gb() -> float:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return max(float(line) for line in out.splitlines() if line.strip()) / 1024
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0


def detect_ram_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal"):
            return float(line.split()[1]) / 1024 / 1024
    return 0.0


def pick_tier(vram_gb: float, config: dict) -> str:
    for name, tier in config["tiers"].items():
        if vram_gb >= tier.get("min_vram_gb", float("inf")):
            return name
    return "cpu"


@functools.lru_cache(maxsize=1)
def resolve() -> dict:
    """Resolved model config: {"tier", "chat", "embeddings"}. Env overrides win."""
    config = _load_config()
    tier = settings.model_tier or pick_tier(detect_vram_gb(), config)
    chat = settings.chat_model or config["tiers"][tier]["chat"]
    return {"tier": tier, "chat": chat, "embeddings": config["embeddings"]}


def chat_model(**kwargs) -> ChatOllama:
    kwargs.setdefault("reasoning", False)  # qwen3 thinking off: big latency win on small GPUs
    return ChatOllama(model=resolve()["chat"], base_url=settings.ollama_base_url, **kwargs)


def embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(model=resolve()["embeddings"], base_url=settings.ollama_base_url)
