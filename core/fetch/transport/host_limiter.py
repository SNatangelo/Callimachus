#!/usr/bin/env python3
# core/fetch/transport/host_limiter.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Shared host-aware rate limiter for synchronous HTTP clients."""

from __future__ import annotations

from dataclasses import dataclass
import atexit
import os
import threading
import time
import urllib.parse

from core.resolve import provider_config


def normalize_host(host: str | None) -> str:
    text = str(host or "").strip().lower()
    if not text:
        return ""
    if "://" in text:
        text = urllib.parse.urlparse(text).netloc or ""
    if text.startswith("www."):
        text = text[4:]
    return text.split(":", 1)[0]


def host_for_url(url: str | None) -> str:
    try:
        host = urllib.parse.urlparse(str(url or "")).hostname
    except ValueError:
        return ""
    return normalize_host(host)


@dataclass(frozen=True)
class HostRate:
    """Token-bucket policy for one host family."""

    refill_per_sec: float
    capacity: float = 1.0

    @classmethod
    def per_second(cls, value: float, *, capacity: float = 1.0) -> "HostRate":
        return cls(refill_per_sec=max(0.0, float(value)), capacity=max(1.0, float(capacity)))

    @classmethod
    def per_minute(cls, value: float, *, capacity: float = 1.0) -> "HostRate":
        return cls.per_second(float(value) / 60.0, capacity=capacity)

    @property
    def unlimited(self) -> bool:
        return self.refill_per_sec <= 0.0


# Adaptive 429 backoff, mirroring the LLM cooldown policy
# (core/verify/claim_evidence/domain/cooldown.py) without importing it: a repeat 429
# on a host doubles the imposed wait up to a ceiling, and once the host answers
# cleanly a few times in a row the wait that worked is learned and seeds the floor of
# the next episode instead of starting over from the baseline.
_ADAPTIVE_BASELINE_SECONDS = 5.0
_ADAPTIVE_MULTIPLIER = 2.0
_ADAPTIVE_MAX_SECONDS = 300.0
_ADAPTIVE_STABLE_SUCCESSES = 2
# AIMD decrease: a host that keeps answering cleanly after an episode probes its
# learned wait back down (halving it), so an overshoot from a past spike does not
# linger. Below the baseline the learned wait is forgotten entirely.
_ADAPTIVE_DECREASE = 0.5
# Persisted learning decays toward nothing so a one-off 429 from days ago cannot
# pessimise a healthy run: after one half-life an untouched host has shed half its
# learned wait, and once it falls below the baseline it is dropped on load.
_ADAPTIVE_DECAY_HALFLIFE_SECONDS = 24 * 3600.0


@dataclass
class _BucketState:
    tokens: float
    last_refill: float
    cooldown_until: float = 0.0
    # Adaptive 429 backoff (see the _ADAPTIVE_* constants). last_applied_seconds is
    # the wait imposed by the current episode; in_cooldown marks that an episode is
    # open so a further 429 escalates and a run of successes can close it;
    # learned_seconds is the wait that proved stable, used to seed the next episode.
    last_applied_seconds: float = 0.0
    in_cooldown: bool = False
    post_cooldown_successes: int = 0
    learned_seconds: float = 0.0
    # Wall-clock epoch when learned_seconds was last set, for cross-run decay
    # (0.0 = never learned). Wall clock, not the monotonic token-bucket clock,
    # because it must be comparable across process runs.
    learned_updated_at: float = 0.0
    learned_deleted: bool = False
    # Every admitted request receives a monotonically increasing token.  A 429
    # covers every request already admitted when it arrives, even if one of
    # their responses arrives after the cooldown has elapsed.
    last_admission_token: int = 0
    cooldown_admission_cutoff: int = 0


class HostCooldownExceeded(RuntimeError):
    """A caller declined to wait for an already-active host cooldown."""

    def __init__(self, host: str, wait_seconds: float) -> None:
        self.host = host
        self.wait_seconds = max(0.0, float(wait_seconds))
        super().__init__(f"host cooldown for {host} requires {self.wait_seconds:g}s")


def _env_names(value) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return []


def _has_api_key(provider: dict, environ: dict[str, str]) -> bool:
    names = _env_names(provider.get("api_key_envs"))
    names.extend(name for name in _env_names(provider.get("api_key_env")) if name not in names)
    for name in names:
        if str(environ.get(name) or "").strip():
            return True
    return False


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rate_from_config(raw_rate: dict | None, *, has_api_key: bool) -> HostRate | None:
    if not isinstance(raw_rate, dict):
        return None
    if has_api_key:
        keyed = _safe_float(raw_rate.get("with_api_key_per_second"))
        if keyed is not None:
            return HostRate.per_second(keyed)
    per_second = _safe_float(raw_rate.get("per_second"))
    if per_second is not None:
        return HostRate.per_second(per_second)
    per_minute = _safe_float(raw_rate.get("per_minute"))
    if per_minute is not None:
        return HostRate.per_minute(per_minute)
    per_seconds = raw_rate.get("per_seconds")
    if isinstance(per_seconds, list) and len(per_seconds) == 2:
        count = _safe_float(per_seconds[0])
        seconds = _safe_float(per_seconds[1])
        if count is not None and seconds is not None and count > 0 and seconds > 0:
            return HostRate.per_second(count / seconds)
    return None


class HostLimiter:
    """Thread-safe per-host token bucket with optional cooldowns."""

    def __init__(
        self,
        *,
        rules: dict[str, HostRate] | None = None,
        default_rate: HostRate | None = None,
        time_fn=None,
        sleep_fn=None,
        wall_time_fn=None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self._rules = {
            normalize_host(host): rate
            for host, rate in (rules or {}).items()
            if normalize_host(host)
        }
        self._default_rate = default_rate or HostRate.per_second(10.0)
        self._time = time_fn or time.monotonic
        self._sleep = sleep_fn or time.sleep
        # Wall clock for cross-run learning timestamps; separate from _time, which
        # is monotonic and only meaningful within one process.
        self._wall = wall_time_fn or time.time
        self._lock = threading.Lock()
        self._states: dict[str, _BucketState] = {}
        self._environ = os.environ if environ is None else environ
        from core.fetch.transport import host_backoff_store

        self._shared_cooldown_store = bool(
            str(self._environ.get(host_backoff_store.ENV_HOST_COOLDOWN_DB) or "").strip()
        )
        from core.fetch.transport import semantic_scholar_pacing

        self._semantic_scholar_pacer = semantic_scholar_pacing.SemanticScholarPacer.from_environ(
            self._environ, wall_time_fn=self._wall
        )
        if self._shared_cooldown_store:
            self._load_shared_cooldowns()

    def _uses_shared_semantic_scholar_pacing(self, host: str) -> bool:
        from core.fetch.transport import semantic_scholar_pacing

        return self._semantic_scholar_pacer is not None and semantic_scholar_pacing.applies(host)

    def rule_for(self, host: str | None) -> HostRate:
        normalized = normalize_host(host)
        if not normalized:
            return self._default_rate
        exact = self._rules.get(normalized)
        if exact is not None:
            return exact
        for suffix, rate in self._rules.items():
            if normalized.endswith(f".{suffix}"):
                return rate
        return self._default_rate

    def acquire(self, host: str | None, *, max_wait_seconds: float | None = None) -> int | None:
        """Admit one request and return its cooldown-wave token.

        ``max_wait_seconds`` limits only this caller's willingness to wait; it
        never changes the host's shared cooldown state.
        """
        normalized = normalize_host(host)
        rule = self.rule_for(normalized)
        started_at = self._time()
        if self._uses_shared_semantic_scholar_pacing(normalized):
            token, wait = self._semantic_scholar_pacer.acquire(
                rate=rule.refill_per_sec, max_wait_seconds=max_wait_seconds
            )
            if token == 0:
                raise HostCooldownExceeded(normalized, wait)
            if wait > 0.0:
                self._sleep(wait)
            return token
        while True:
            with self._lock:
                now = self._time()
                state = self._states.get(normalized)
                if state is None:
                    state = _BucketState(tokens=rule.capacity, last_refill=now)
                    self._states[normalized] = state
                if rule.unlimited:
                    state.last_admission_token += 1
                    return state.last_admission_token
                self._refill(state, rule, now)
                if now < state.cooldown_until:
                    wait = state.cooldown_until - now
                elif state.tokens >= 1.0:
                    state.tokens -= 1.0
                    state.last_admission_token += 1
                    return state.last_admission_token
                else:
                    wait = (1.0 - state.tokens) / rule.refill_per_sec
                if max_wait_seconds is not None and now - started_at + wait > max_wait_seconds:
                    raise HostCooldownExceeded(normalized, wait)
            if wait > 0:
                self._sleep(wait)

    def cooldown(
        self,
        host: str | None,
        retry_after: float = 0.0,
        *,
        admission_token: int | None = None,
    ) -> None:
        """Pause a host after a 429, escalating on a repeat within the same episode.

        ``retry_after`` is the server's stated wait (0 when it gave none). It is a
        floor, not the whole story: a first 429 waits at least the baseline, and each
        further 429 before the host recovers doubles the previous wait up to the
        ceiling, so a host that keeps refusing is backed off instead of hammered.
        """
        normalized = normalize_host(host)
        if not normalized:
            return
        if self._uses_shared_semantic_scholar_pacing(normalized):
            self._semantic_scholar_pacer.cooldown(
                retry_after=retry_after, admission_token=admission_token
            )
            return
        floor = max(0.0, float(retry_after or 0.0))
        with self._lock:
            now = self._time()
            state = self._states.get(normalized)
            if state is None:
                state = _BucketState(tokens=self.rule_for(normalized).capacity, last_refill=now)
                self._states[normalized] = state
            self._refill(state, self.rule_for(normalized), now)
            if admission_token is not None and admission_token <= state.cooldown_admission_cutoff:
                # A late 429 from an already-admitted wave is not a retry.  It
                # may raise the server floor, but never starts a fresh episode.
                if floor > state.last_applied_seconds:
                    state.last_applied_seconds = min(floor, _ADAPTIVE_MAX_SECONDS)
                    state.cooldown_until = max(state.cooldown_until, now + state.last_applied_seconds)
                    self._save_shared_cooldown(normalized, state, now)
                return
            if state.in_cooldown and state.last_applied_seconds > 0.0:
                escalated = min(state.last_applied_seconds * _ADAPTIVE_MULTIPLIER, _ADAPTIVE_MAX_SECONDS)
                applied = max(escalated, floor)
            else:
                # First 429 of an episode: honour Retry-After, but never start below
                # the wait this host taught us last time, and keep the baseline floor
                # so an absent Retry-After still pauses.
                applied = max(floor, state.learned_seconds) or _ADAPTIVE_BASELINE_SECONDS
            applied = min(applied, _ADAPTIVE_MAX_SECONDS)
            if applied <= 0.0:
                return
            state.last_applied_seconds = applied
            state.in_cooldown = True
            state.post_cooldown_successes = 0
            state.cooldown_until = max(state.cooldown_until, now + applied)
            if admission_token is not None:
                state.cooldown_admission_cutoff = state.last_admission_token
            self._save_shared_cooldown(normalized, state, now)

    def report_success(self, host: str | None) -> None:
        """Record a clean response, driving the host's learned backoff.

        While an episode is open, enough consecutive successes promote the wait that
        got us through to ``learned_seconds`` and close the episode, so the next 429
        starts from what worked rather than the baseline. Once the episode is closed
        but a learned wait is still carried, further sustained successes halve it
        (AIMD decrease) until it drops below the baseline and is forgotten — so an
        overshoot from a past spike does not linger.
        """
        normalized = normalize_host(host)
        if not normalized:
            return
        if self._uses_shared_semantic_scholar_pacing(normalized):
            self._semantic_scholar_pacer.report_success()
            return
        with self._lock:
            state = self._states.get(normalized)
            if state is None:
                return
            if state.in_cooldown:
                state.post_cooldown_successes += 1
                if state.post_cooldown_successes >= _ADAPTIVE_STABLE_SUCCESSES:
                    state.learned_seconds = state.last_applied_seconds
                    state.learned_updated_at = self._wall()
                    state.in_cooldown = False
                    state.post_cooldown_successes = 0
                self._save_shared_cooldown(normalized, state, self._time())
                return
            if state.learned_seconds > 0.0:
                state.post_cooldown_successes += 1
                if state.post_cooldown_successes >= _ADAPTIVE_STABLE_SUCCESSES:
                    state.post_cooldown_successes = 0
                    reduced = state.learned_seconds * _ADAPTIVE_DECREASE
                    if reduced < _ADAPTIVE_BASELINE_SECONDS:
                        state.learned_seconds = 0.0
                        state.learned_updated_at = 0.0
                        state.learned_deleted = True
                    else:
                        state.learned_seconds = reduced
                        state.learned_updated_at = self._wall()
                self._save_shared_cooldown(normalized, state, self._time())

    def cooldown_until(self, host: str | None) -> float:
        normalized = normalize_host(host)
        if self._uses_shared_semantic_scholar_pacing(normalized):
            return float(self._semantic_scholar_pacer.snapshot()["cooldown_until"])
        with self._lock:
            state = self._states.get(normalized)
            return 0.0 if state is None else state.cooldown_until

    def acquire_for_url(self, url: str | None, *, max_wait_seconds: float | None = None) -> int | None:
        return self.acquire(host_for_url(url), max_wait_seconds=max_wait_seconds)

    def acquire_for_fetch_url(self, url: str | None) -> int | None:
        """Admit Fetch I/O, declining an active cooldown but not normal pacing.

        The check and reservation share the limiter's critical section.  This
        avoids the ``cooldown_until``/``acquire`` race while still allowing the
        short configured token interval needed by cookie warmup and replay.
        """
        normalized = normalize_host(host_for_url(url))
        rule = self.rule_for(normalized)
        if self._uses_shared_semantic_scholar_pacing(normalized):
            token, wait = self._semantic_scholar_pacer.acquire(
                rate=rule.refill_per_sec,
                max_wait_seconds=None,
                decline_active_cooldown=True,
            )
            if token == 0:
                raise HostCooldownExceeded(normalized, wait)
            if wait > 0.0:
                self._sleep(wait)
            return token
        while True:
            with self._lock:
                now = self._time()
                state = self._states.get(normalized)
                if state is None:
                    state = _BucketState(tokens=rule.capacity, last_refill=now)
                    self._states[normalized] = state
                self._refill(state, rule, now)
                if now < state.cooldown_until:
                    raise HostCooldownExceeded(normalized, state.cooldown_until - now)
                if rule.unlimited:
                    state.last_admission_token += 1
                    return state.last_admission_token
                if state.tokens >= 1.0:
                    state.tokens -= 1.0
                    state.last_admission_token += 1
                    return state.last_admission_token
                wait = (1.0 - state.tokens) / rule.refill_per_sec
            if wait > 0:
                self._sleep(wait)

    def cooldown_for_url(
        self,
        url: str | None,
        seconds: float = 0.0,
        *,
        admission_token: int | None = None,
    ) -> None:
        self.cooldown(host_for_url(url), seconds, admission_token=admission_token)

    def report_success_for_url(self, url: str | None) -> None:
        self.report_success(host_for_url(url))

    @staticmethod
    def _decay(learned: float, updated_at: float, now: float) -> float:
        """The learned wait faded for the time elapsed since it was last set."""
        if updated_at <= 0.0 or now <= updated_at:
            return learned
        age = now - updated_at
        return learned * (_ADAPTIVE_DECREASE ** (age / _ADAPTIVE_DECAY_HALFLIFE_SECONDS))

    def export_learned(self) -> dict[str, dict[str, float]]:
        """Per-host learned backoff worth persisting across runs.

        A recovered value is exported as an explicit deletion so a per-host
        merge does not reseed it on restart.
        """
        with self._lock:
            return {
                host: {
                    "learned_seconds": state.learned_seconds if state.learned_seconds > 0.0 else 0.0,
                    "updated_at": state.learned_updated_at,
                }
                for host, state in self._states.items()
                if state.learned_seconds > 0.0 or state.learned_deleted
            }

    def load_learned(self, mapping: dict | None) -> None:
        """Seed learned backoff from a prior run, decayed for the time elapsed.

        A value that has decayed below the baseline is dropped rather than carried:
        a one-off 429 from days ago must not slow a host that has since been healthy.
        The surviving value is re-stamped to now so it is not decayed twice.
        """
        if not mapping:
            return
        now = self._wall()
        with self._lock:
            for host, record in mapping.items():
                normalized = normalize_host(host)
                if not normalized or not isinstance(record, dict):
                    continue
                try:
                    learned = float(record.get("learned_seconds") or 0.0)
                    updated_at = float(record.get("updated_at") or 0.0)
                except (TypeError, ValueError):
                    continue
                if learned <= 0.0:
                    continue
                effective = self._decay(learned, updated_at, now)
                if effective < _ADAPTIVE_BASELINE_SECONDS:
                    continue
                state = self._states.get(normalized)
                if state is None:
                    state = _BucketState(tokens=self.rule_for(normalized).capacity, last_refill=self._time())
                    self._states[normalized] = state
                state.learned_seconds = min(effective, _ADAPTIVE_MAX_SECONDS)
                state.learned_updated_at = now
                state.learned_deleted = False

    def snapshot(self) -> dict[str, dict[str, float]]:
        with self._lock:
            return {
                host: {
                    "tokens": state.tokens,
                    "last_refill": state.last_refill,
                    "cooldown_until": state.cooldown_until,
                    "last_applied_seconds": state.last_applied_seconds,
                    "learned_seconds": state.learned_seconds,
                    "learned_updated_at": state.learned_updated_at,
                    "in_cooldown": state.in_cooldown,
                    "post_cooldown_successes": state.post_cooldown_successes,
                }
                for host, state in self._states.items()
            }

    def _refill(self, state: _BucketState, rule: HostRate, now: float) -> None:
        elapsed = max(0.0, now - state.last_refill)
        if elapsed > 0 and rule.refill_per_sec > 0:
            state.tokens = min(rule.capacity, state.tokens + (elapsed * rule.refill_per_sec))
        state.last_refill = now

    def _load_shared_cooldowns(self) -> None:
        from core.fetch.transport import host_backoff_store

        records = host_backoff_store.load_cooldowns(environ=self._environ)
        now = self._time()
        wall_now = self._wall()
        for host, record in records.items():
            normalized = normalize_host(host)
            self._states[normalized] = _BucketState(
                tokens=self.rule_for(normalized).capacity,
                last_refill=now,
                cooldown_until=now + max(0.0, record["cooldown_until"] - wall_now),
                last_applied_seconds=record["last_applied_seconds"],
                in_cooldown=record["in_cooldown"],
                post_cooldown_successes=record["post_cooldown_successes"],
                learned_seconds=record["learned_seconds"],
                learned_updated_at=record["learned_updated_at"],
            )

    def _save_shared_cooldown(self, host: str, state: _BucketState, now: float) -> None:
        if not self._shared_cooldown_store:
            return
        from core.fetch.transport import host_backoff_store

        host_backoff_store.save_cooldown(
            host,
            {
                "cooldown_until": self._wall() + max(0.0, state.cooldown_until - now),
                "last_applied_seconds": state.last_applied_seconds,
                "in_cooldown": state.in_cooldown,
                "post_cooldown_successes": state.post_cooldown_successes,
                "learned_seconds": state.learned_seconds,
                "learned_updated_at": state.learned_updated_at,
            },
            environ=self._environ,
        )


def build_default_host_limiter(
    *,
    environ: dict[str, str] | None = None,
    time_fn=None,
    sleep_fn=None,
) -> HostLimiter:
    env = os.environ if environ is None else environ
    config = provider_config.load(environ=env)
    providers = config.get("providers")
    provider_map = providers if isinstance(providers, dict) else {}
    hosts = config.get("hosts")
    host_map = hosts if isinstance(hosts, dict) else {}
    default_rate = _rate_from_config(config.get("default_rate"), has_api_key=False)
    rules: dict[str, HostRate] = {}
    for host, provider_name in host_map.items():
        if not isinstance(host, str) or not isinstance(provider_name, str):
            continue
        provider = provider_map.get(provider_name.strip().lower())
        if not isinstance(provider, dict):
            continue
        rate = _rate_from_config(provider.get("rate"), has_api_key=_has_api_key(provider, env))
        if rate is None:
            continue
        normalized = normalize_host(host)
        if normalized:
            rules[normalized] = rate
    limiter = HostLimiter(
        rules=rules,
        default_rate=default_rate or HostRate.per_second(10.0),
        time_fn=time_fn,
        sleep_fn=sleep_fn,
        environ=env,
    )
    try:
        from core.fetch.transport import host_backoff_store

        limiter.load_learned(host_backoff_store.load(environ=env))
    except Exception:
        # Backoff memory is an optimisation; a failed load just starts fresh.
        pass
    return limiter


_SHARED_LIMITER: HostLimiter | None = None
_SHARED_LIMITER_LOCK = threading.Lock()


def get_shared_limiter(environ: dict[str, str] | None = None) -> HostLimiter:
    global _SHARED_LIMITER
    with _SHARED_LIMITER_LOCK:
        if _SHARED_LIMITER is None:
            _SHARED_LIMITER = build_default_host_limiter(environ=environ)
        return _SHARED_LIMITER


def set_shared_limiter(limiter: HostLimiter | None) -> None:
    global _SHARED_LIMITER
    with _SHARED_LIMITER_LOCK:
        _SHARED_LIMITER = limiter


def reset_shared_limiter() -> None:
    set_shared_limiter(None)


def _persist_learned_at_exit() -> None:
    """Flush the shared limiter's learned backoff at process exit.

    Registered once. A no-op unless a real run actually learned something, so test
    processes (which build the shared limiter but rarely reach a stable post-429
    recovery) leave no file behind.
    """
    limiter = _SHARED_LIMITER
    if limiter is None:
        return
    try:
        from core.fetch.transport import host_backoff_store

        host_backoff_store.save(limiter.export_learned())
    except Exception:
        pass


atexit.register(_persist_learned_at_exit)
