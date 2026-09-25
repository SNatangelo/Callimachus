# core/verify/backends/glm.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""GLM (ZhipuAI / z.ai) backend — Chat Completions API."""
import os

from core.verify.backends._registry import BackendSpec, register
from core.verify.backends._chat_transport import call_chat_completions

ENV_ZHIPUAI_KEY = "ZHIPUAI_API_KEY"
ENV_ZHIPUAI_MODEL = "ZHIPUAI_MODEL"


def _available() -> bool:
    return bool(os.environ.get(ENV_ZHIPUAI_KEY))


SPEC = BackendSpec(
    name="glm",
    env_key=ENV_ZHIPUAI_KEY,
    env_model=ENV_ZHIPUAI_MODEL,
    base_url="https://open.bigmodel.cn/api/paas/v4",
    available=_available,
    auto_priority=85,
)


def _call_glm_api(system: str, user: str, model: str | None, max_tokens: int = 1500) -> str:
    return call_chat_completions(system, user, model, max_tokens, spec=SPEC)


SPEC.call = _call_glm_api
register(SPEC)
