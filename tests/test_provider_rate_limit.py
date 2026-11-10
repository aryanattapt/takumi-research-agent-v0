from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base
from takumi_research_agent.providers.rate_limit import (
    provider_quota_key,
    reserve_provider_slot,
)


def test_database_rate_gate_reserves_spaced_slots_for_same_credential():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    key_hash = provider_quota_key("openai_compatible", "https://model.example/v1/", "secret-key")

    assert reserve_provider_slot(sessions, key_hash=key_hash, requests_per_minute=60, now=100) == 0
    assert reserve_provider_slot(sessions, key_hash=key_hash, requests_per_minute=60, now=100) == 1
    assert reserve_provider_slot(sessions, key_hash=key_hash, requests_per_minute=60, now=100) == 2
    engine.dispose()


def test_rate_gate_key_normalizes_endpoint_and_never_contains_credential():
    first = provider_quota_key("openai_compatible", "https://model.example/v1/", "secret-key")
    second = provider_quota_key("openai_compatible", "https://MODEL.example/v1", "secret-key")

    assert first == second
    assert "secret-key" not in first
    assert len(first) == 64


def test_rate_gate_slots_are_independent_for_distinct_credentials():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    first_key = provider_quota_key("openai_compatible", "https://model.example/v1", "key-one")
    second_key = provider_quota_key("openai_compatible", "https://model.example/v1", "key-two")

    reserve_provider_slot(sessions, key_hash=first_key, requests_per_minute=30, now=200)
    assert reserve_provider_slot(sessions, key_hash=second_key, requests_per_minute=30, now=200) == 0
    engine.dispose()
