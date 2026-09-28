"""Codex -900k context variants keep their base model's reasoning ladder."""
import pytest

from api import config as cfg
from api.gateway_chat import _gateway_reasoning_effort_for_request

CODEX_900K_MODELS = ("gpt-5.6-sol-900k", "gpt-5.6-terra-900k", "gpt-5.6-luna-900k")


@pytest.mark.parametrize("model_id", CODEX_900K_MODELS)
def test_codex_900k_variants_advertise_and_preserve_max(model_id):
    assert "max" in cfg.resolve_model_reasoning_efforts(model_id, provider_id="openai-codex")
    assert cfg.coerce_reasoning_effort_for_model("max", model_id, provider_id="openai-codex") == "max"
    gateway_cfg = {"agent": {"reasoning_effort": "max"}}
    assert _gateway_reasoning_effort_for_request(
        gateway_cfg, model=model_id, model_provider="openai-codex"
    ) == "max"


@pytest.mark.parametrize(
    "model_id,provider_id",
    [
        ("gpt-5.6-luna-900k", "openai"),
        ("gpt-5.6-luna-pro-900k", "openai-codex"),
        ("gpt-5.6-nebula-900k", "openai-codex"),
    ],
)
def test_unsupported_900k_aliases_remain_capped_at_xhigh(model_id, provider_id):
    assert "max" not in cfg.resolve_model_reasoning_efforts(model_id, provider_id=provider_id)
    assert cfg.coerce_reasoning_effort_for_model("max", model_id, provider_id=provider_id) == "xhigh"


def test_provider_hinted_900k_variant_advertises_max():
    assert "max" in cfg.resolve_model_reasoning_efforts(
        "@openai-codex:gpt-5.6-luna-900k", provider_id="openai-codex"
    )
