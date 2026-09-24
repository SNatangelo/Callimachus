# tests/test_semantic_scholar_rate_floor.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
from core.fetch.transport import host_limiter


def test_semantic_scholar_default_rate_is_one_request_every_two_seconds():
    host_limiter.reset_shared_limiter()
    try:
        limiter = host_limiter.get_shared_limiter(environ={})
        rate = limiter.rule_for("api.semanticscholar.org")
        assert rate.refill_per_sec == 0.5
        assert rate.capacity == 1.0
    finally:
        host_limiter.reset_shared_limiter()
