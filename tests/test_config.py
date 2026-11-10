import pytest

from takumi_research_agent.config import Settings


def test_shared_provider_rate_limit_setting_is_parsed(monkeypatch):
    monkeypatch.setenv("PROVIDER_REQUESTS_PER_MINUTE", "24")
    settings = Settings.from_env(env_file=None)

    assert settings.provider_requests_per_minute == 24


def test_semantic_history_requires_model_and_key_together(monkeypatch):
    monkeypatch.setenv("HISTORY_EMBEDDING_MODEL", "multilingual-model")
    monkeypatch.delenv("HISTORY_EMBEDDING_API_KEY", raising=False)

    with pytest.raises(ValueError, match="must be configured together"):
        Settings.from_env(env_file=None)


def test_semantic_history_configuration_accepts_threshold_and_endpoint(monkeypatch):
    monkeypatch.setenv("HISTORY_EMBEDDING_MODEL", "multilingual-model")
    monkeypatch.setenv("HISTORY_EMBEDDING_API_KEY", "test-secret")
    monkeypatch.setenv("HISTORY_EMBEDDING_BASE_URL", "https://embeddings.example/v1")
    monkeypatch.setenv("HISTORY_EMBEDDING_MIN_SIMILARITY", "0.82")

    settings = Settings.from_env(env_file=None)

    assert settings.history_embedding_model == "multilingual-model"
    assert settings.history_embedding_base_url == "https://embeddings.example/v1"
    assert settings.history_embedding_min_similarity == 0.82
