from app.llm import _load_config, pick_tier

CONFIG = _load_config()


def test_tier_boundaries():
    assert pick_tier(24.0, CONFIG) == "gpu_24"
    assert pick_tier(20.0, CONFIG) == "gpu_24"
    assert pick_tier(16.0, CONFIG) == "gpu_16"
    assert pick_tier(8.0, CONFIG) == "gpu_8"       # this dev machine (RTX 2070)
    assert pick_tier(6.0, CONFIG) == "gpu_8"
    assert pick_tier(5.9, CONFIG) == "cpu"
    assert pick_tier(0.0, CONFIG) == "cpu"


def test_all_tiers_define_chat_model():
    for tier in CONFIG["tiers"].values():
        assert tier["chat"]
    assert CONFIG["embeddings"] == "nomic-embed-text"  # must match vector(768) in schema


def test_env_overrides(monkeypatch):
    from app import llm
    from app.config import settings

    monkeypatch.setattr(settings, "model_tier", "cpu")
    monkeypatch.setattr(settings, "chat_model", "")
    llm.resolve.cache_clear()
    assert llm.resolve()["chat"] == CONFIG["tiers"]["cpu"]["chat"]

    monkeypatch.setattr(settings, "chat_model", "my-custom:latest")
    llm.resolve.cache_clear()
    assert llm.resolve()["chat"] == "my-custom:latest"
    llm.resolve.cache_clear()
