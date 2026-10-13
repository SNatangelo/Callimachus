#!/usr/bin/env python3
# tests/test_host_limiter.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Unit tests for the shared per-host rate limiter."""

from tests._bootstrap import *  # noqa: F401,F403

from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
from unittest import mock

from core.fetch.transport import host_limiter
from core.resolve import provider_config


class _FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        wait = round(float(seconds), 3)
        self.sleeps.append(wait)
        self.now += float(seconds)


class TestHostLimiter(unittest.TestCase):
    def tearDown(self):
        provider_config.reset_for_tests()
        host_limiter.reset_shared_limiter()

    def test_same_host_waits_for_next_token(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"openalex.org": host_limiter.HostRate.per_second(2.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        limiter.acquire("api.openalex.org")
        limiter.acquire("api.openalex.org")
        limiter.acquire("api.openalex.org")

        self.assertEqual(clock.sleeps, [0.5, 0.5])
        self.assertAlmostEqual(clock.now, 1.0, places=3)

    def test_host_for_url_uses_canonical_hostname_without_userinfo_or_port(self):
        self.assertEqual(
            host_limiter.host_for_url("https://user:secret@www.Example.test:8443/path"),
            "example.test",
        )
        self.assertEqual(host_limiter.host_for_url("http://[broken"), "")

    def test_different_hosts_do_not_block_each_other(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={
                "openalex.org": host_limiter.HostRate.per_second(2.0),
                "crossref.org": host_limiter.HostRate.per_second(2.0),
            },
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        limiter.acquire("api.openalex.org")
        limiter.acquire("api.crossref.org")

        self.assertEqual(clock.sleeps, [])
        self.assertEqual(clock.now, 0.0)

        limiter.acquire("api.openalex.org")
        self.assertEqual(clock.sleeps, [0.5])
        self.assertAlmostEqual(clock.now, 0.5, places=3)

    def test_cooldown_blocks_until_retry_after(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"openalex.org": host_limiter.HostRate.per_second(1000.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        limiter.acquire("api.openalex.org")
        limiter.cooldown("api.openalex.org", 3.0)
        limiter.acquire("api.openalex.org")

        self.assertEqual(clock.sleeps, [3.0])
        self.assertAlmostEqual(
            limiter.cooldown_until("api.openalex.org"),
            3.0,
            places=3,
        )

    def test_build_default_host_limiter_reads_rate_from_config(self):
        config_path = self._write_config(
            {
                "providers": {
                    "demo": {
                        "enabled": True,
                        "rate": {"per_second": 7},
                    }
                },
                "hosts": {
                    "demo.example.org": "demo",
                },
            }
        )
        saved_path = provider_config._CONFIG_PATH
        try:
            provider_config._CONFIG_PATH = Path(config_path)
            provider_config.reset_for_tests()
            limiter = host_limiter.build_default_host_limiter(environ={})
        finally:
            provider_config._CONFIG_PATH = saved_path
            provider_config.reset_for_tests()
        self.assertAlmostEqual(limiter.rule_for("demo.example.org").refill_per_sec, 7.0, places=6)

    def test_semantic_scholar_rate_is_one_request_every_two_seconds(self):
        production = host_limiter.build_default_host_limiter(environ={})
        production_rate = production.rule_for("api.semanticscholar.org")
        self.assertAlmostEqual(production_rate.capacity, 1.0, places=6)
        self.assertAlmostEqual(production_rate.refill_per_sec, 0.5, places=6)

        with tempfile.TemporaryDirectory() as config_dir:
            fallback = provider_config.load(path=Path(config_dir) / "missing.json")
        fallback_rate = fallback["providers"]["semantic_scholar"]["rate"]
        fallback_rule = host_limiter._rate_from_config(fallback_rate, has_api_key=False)
        self.assertIsNotNone(fallback_rule)
        self.assertAlmostEqual(fallback_rule.capacity, 1.0, places=6)
        self.assertAlmostEqual(fallback_rule.refill_per_sec, 0.5, places=6)

    def test_unknown_host_uses_configured_default_rate(self):
        limiter = host_limiter.build_default_host_limiter(environ={})
        self.assertAlmostEqual(limiter.rule_for("unknown.example.org").refill_per_sec, 10.0, places=6)

    def test_default_rate_can_be_overridden_from_config(self):
        config_path = self._write_config(
            {
                "default_rate": {
                    "per_second": 4,
                }
            }
        )
        saved_path = provider_config._CONFIG_PATH
        try:
            provider_config._CONFIG_PATH = Path(config_path)
            provider_config.reset_for_tests()
            limiter = host_limiter.build_default_host_limiter(environ={})
        finally:
            provider_config._CONFIG_PATH = saved_path
            provider_config.reset_for_tests()
        self.assertAlmostEqual(limiter.rule_for("unknown.example.org").refill_per_sec, 4.0, places=6)

    def test_build_default_host_limiter_uses_api_key_variant_from_config(self):
        no_key = host_limiter.build_default_host_limiter(environ={})
        with_key = host_limiter.build_default_host_limiter(environ={"OPENALEX_API_KEY": "key"})
        with_ncbi = host_limiter.build_default_host_limiter(environ={"NCBI_API_KEY": "key"})
        with_entrez = host_limiter.build_default_host_limiter(environ={"ENTREZ_API_KEY": "key"})

        self.assertAlmostEqual(no_key.rule_for("api.openalex.org").refill_per_sec, 5.0, places=6)
        self.assertAlmostEqual(with_key.rule_for("api.openalex.org").refill_per_sec, 30.0, places=6)
        self.assertAlmostEqual(no_key.rule_for("api.openalex.org").capacity, 1.0, places=6)
        self.assertAlmostEqual(
            host_limiter.build_default_host_limiter(environ={}).rule_for("eutils.ncbi.nlm.nih.gov").refill_per_sec,
            3.0,
            places=6,
        )
        self.assertAlmostEqual(
            with_ncbi.rule_for("eutils.ncbi.nlm.nih.gov").refill_per_sec,
            10.0,
            places=6,
        )
        self.assertAlmostEqual(
            with_entrez.rule_for("eutils.ncbi.nlm.nih.gov").refill_per_sec,
            10.0,
            places=6,
        )
        self.assertAlmostEqual(
            no_key.rule_for("api.springernature.com").refill_per_sec,
            1.0,
            places=6,
        )
        self.assertAlmostEqual(
            no_key.rule_for("api.wiley.com").refill_per_sec,
            0.1,
            places=6,
        )

    def test_crossref_family_host_keeps_suffix_match(self):
        limiter = host_limiter.build_default_host_limiter(environ={})
        self.assertAlmostEqual(limiter.rule_for("api.crossref.org").refill_per_sec, 5.0, places=6)
        self.assertAlmostEqual(limiter.rule_for("api.labs.crossref.org").refill_per_sec, 5.0, places=6)

    def test_repeated_429_escalates_the_cooldown(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"s2.org": host_limiter.HostRate.per_second(1000.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        # No Retry-After: the first 429 waits the baseline, each repeat in the same
        # episode doubles it. A host that keeps refusing is backed off, not hammered.
        limiter.cooldown("api.s2.org", 0.0)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 5.0, places=3)
        limiter.cooldown("api.s2.org", 0.0)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 10.0, places=3)
        limiter.cooldown("api.s2.org", 0.0)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 20.0, places=3)

    def test_late_429_from_admitted_wave_does_not_escalate(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"s2.org": host_limiter.HostRate.per_second(1000.0, capacity=10.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0, capacity=10.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        first = limiter.acquire("api.s2.org")
        concurrent = limiter.acquire("api.s2.org")
        limiter.cooldown("api.s2.org", 0.0, admission_token=first)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 5.0, places=3)

        # This concurrent request was admitted before the first 429.  Its slow
        # response arrives after the first cooldown ends, so it must not look
        # like a retry and double the shared cooldown.
        clock.now = 5.001
        limiter.cooldown("api.s2.org", 0.0, admission_token=concurrent)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 5.0, places=3)

        retry = limiter.acquire("api.s2.org")
        retry_concurrent = limiter.acquire("api.s2.org")
        limiter.cooldown("api.s2.org", 0.0, admission_token=retry)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 15.001, places=3)
        limiter.cooldown("api.s2.org", 0.0, admission_token=retry_concurrent)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org"), 15.001, places=3)

    def test_learns_cooldown_and_seeds_the_next_episode(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"s2.org": host_limiter.HostRate.per_second(1000.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
        )

        limiter.cooldown("api.s2.org", 0.0)   # 5s
        limiter.cooldown("api.s2.org", 0.0)   # 10s
        limiter.report_success("api.s2.org")  # one clean answer: episode still open
        self.assertTrue(limiter.snapshot()["api.s2.org"]["in_cooldown"])

        limiter.report_success("api.s2.org")  # steady now: 10s is learned, episode ends
        snap = limiter.snapshot()["api.s2.org"]
        self.assertFalse(snap["in_cooldown"])
        self.assertAlmostEqual(snap["learned_seconds"], 10.0, places=3)

        # A later 429 starts from the learned 10s, not the 5s baseline.
        clock.now = 100.0
        limiter.cooldown("api.s2.org", 0.0)
        self.assertAlmostEqual(limiter.cooldown_until("api.s2.org") - clock.now, 10.0, places=3)

    def test_sustained_success_probes_the_learned_wait_back_down(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"s2.org": host_limiter.HostRate.per_second(1000.0)},
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
            wall_time_fn=lambda: 1000.0,
        )

        # Escalate to 20s, then recover: 20s is learned.
        limiter.cooldown("api.s2.org", 0.0)   # 5
        limiter.cooldown("api.s2.org", 0.0)   # 10
        limiter.cooldown("api.s2.org", 0.0)   # 20
        limiter.report_success("api.s2.org")
        limiter.report_success("api.s2.org")
        self.assertAlmostEqual(limiter.snapshot()["api.s2.org"]["learned_seconds"], 20.0, places=3)

        # Sustained clean traffic halves the learned wait toward the minimum needed.
        limiter.report_success("api.s2.org")
        limiter.report_success("api.s2.org")
        self.assertAlmostEqual(limiter.snapshot()["api.s2.org"]["learned_seconds"], 10.0, places=3)

        # Kept answering cleanly: it decays below the baseline and is forgotten.
        for _ in range(6):
            limiter.report_success("api.s2.org")
        self.assertEqual(limiter.snapshot()["api.s2.org"]["learned_seconds"], 0.0)

    def test_load_decays_learning_and_drops_stale_entries(self):
        half_life = host_limiter._ADAPTIVE_DECAY_HALFLIFE_SECONDS
        now = 1_000_000.0
        limiter = host_limiter.HostLimiter(
            default_rate=host_limiter.HostRate.per_second(1000.0),
            wall_time_fn=lambda: now,
        )

        limiter.load_learned({
            "fresh.org": {"learned_seconds": 40.0, "updated_at": now},
            "aged.org": {"learned_seconds": 40.0, "updated_at": now - half_life},
            "stale.org": {"learned_seconds": 8.0, "updated_at": now - 4 * half_life},
        })
        snap = limiter.snapshot()

        self.assertAlmostEqual(snap["fresh.org"]["learned_seconds"], 40.0, places=3)
        self.assertAlmostEqual(snap["aged.org"]["learned_seconds"], 20.0, places=3)  # one half-life
        self.assertNotIn("stale.org", snap)  # decayed below baseline, dropped
        # Survivors are re-stamped to now so a later run does not decay them twice.
        self.assertAlmostEqual(snap["aged.org"]["learned_updated_at"], now, places=3)

    def test_loaded_learning_seeds_the_first_cooldown(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            default_rate=host_limiter.HostRate.per_second(1000.0),
            time_fn=clock.monotonic,
            sleep_fn=clock.sleep,
            wall_time_fn=lambda: 500.0,
        )
        limiter.load_learned({"s2.org": {"learned_seconds": 25.0, "updated_at": 500.0}})

        limiter.cooldown("s2.org", 0.0)  # first 429 starts from the learned 25s, not baseline
        self.assertAlmostEqual(limiter.cooldown_until("s2.org"), 25.0, places=3)

    def test_backoff_store_round_trips_in_a_dedicated_sqlite_database(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
        mapping = {"api.s2.org": {"learned_seconds": 12.0, "updated_at": 111.0}}

        host_backoff_store.save(mapping, environ=env)
        self.assertEqual(host_backoff_store.load(environ=env), mapping)
        self.assertTrue(Path(host_backoff_store._path(env)).as_posix().endswith("storage/host_backoff.sqlite"))
        with sqlite3.connect(host_backoff_store._path(env)) as conn:
            self.assertEqual(
                conn.execute("SELECT host, learned_seconds, updated_at FROM host_backoff").fetchall(),
                [("api.s2.org", 12.0, 111.0)],
            )

    def test_backoff_store_closes_each_owned_connection(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir))
        db_path = str(Path(state_dir) / "suite" / "cooldowns.sqlite")
        env = {
            "CITATION_VERIFIER_STATE_DIR": state_dir,
            host_backoff_store.ENV_HOST_COOLDOWN_DB: db_path,
        }
        opened = []
        failure = {"prefix": None}
        real_connect = sqlite3.connect

        class TrackedConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.was_closed = False

            def _fail_if_requested(self, sql):
                prefix = failure["prefix"]
                if prefix and sql.lstrip().startswith(prefix):
                    failure["prefix"] = None
                    raise sqlite3.OperationalError("synthetic store I/O failure")

            def execute(self, sql, parameters=()):
                self._fail_if_requested(sql)
                return super().execute(sql, parameters)

            def executemany(self, sql, parameters):
                self._fail_if_requested(sql)
                return super().executemany(sql, parameters)

            def close(self):
                self.was_closed = True
                return super().close()

        def tracked_connect(*args, **kwargs):
            kwargs["factory"] = TrackedConnection
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def invoke_and_assert_closed(call, *, expected_error=None):
            prior = len(opened)
            if expected_error is None:
                result = call()
            else:
                with self.assertRaises(expected_error):
                    call()
                result = None
            self.assertEqual(len(opened), prior + 1)
            self.assertTrue(opened[-1].was_closed)
            return result

        mapping = {"api.s2.org": {"learned_seconds": 12.0, "updated_at": 111.0}}
        cooldown = {
            "cooldown_until": 10.0,
            "last_applied_seconds": 2.0,
            "in_cooldown": True,
            "post_cooldown_successes": 0,
            "learned_seconds": 0.0,
            "learned_updated_at": 0.0,
        }
        with mock.patch.object(host_backoff_store.sqlite3, "connect", side_effect=tracked_connect):
            self.assertEqual(invoke_and_assert_closed(lambda: host_backoff_store.load(environ=env)), {})
            invoke_and_assert_closed(lambda: host_backoff_store.save(mapping, environ=env))
            self.assertEqual(invoke_and_assert_closed(lambda: host_backoff_store.load(environ=env)), mapping)
            self.assertEqual(
                invoke_and_assert_closed(lambda: host_backoff_store.load_cooldowns(environ=env)),
                {},
            )
            invoke_and_assert_closed(
                lambda: host_backoff_store.save_cooldown("api.s2.org", cooldown, environ=env),
            )
            self.assertEqual(
                invoke_and_assert_closed(lambda: host_backoff_store.load_cooldowns(environ=env)),
                {"api.s2.org": cooldown},
            )

            failure["prefix"] = "SELECT host, learned_seconds"
            self.assertEqual(invoke_and_assert_closed(lambda: host_backoff_store.load(environ=env)), {})
            failure["prefix"] = "INSERT INTO host_backoff"
            invoke_and_assert_closed(lambda: host_backoff_store.save(mapping, environ=env))
            failure["prefix"] = "SELECT host, cooldown_until"
            invoke_and_assert_closed(
                lambda: host_backoff_store.load_cooldowns(environ=env),
                expected_error=host_backoff_store.HostCooldownStoreError,
            )
            failure["prefix"] = "INSERT INTO host_cooldown"
            invoke_and_assert_closed(
                lambda: host_backoff_store.save_cooldown("api.s2.org", cooldown, environ=env),
                expected_error=host_backoff_store.HostCooldownStoreError,
            )

    def test_backoff_store_disjoint_saves_merge(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
        host_backoff_store.save({"a.org": {"learned_seconds": 10.0, "updated_at": 1.0}}, environ=env)
        host_backoff_store.save({"b.org": {"learned_seconds": 20.0, "updated_at": 2.0}}, environ=env)
        self.assertEqual(
            host_backoff_store.load(environ=env),
            {
                "a.org": {"learned_seconds": 10.0, "updated_at": 1.0},
                "b.org": {"learned_seconds": 20.0, "updated_at": 2.0},
            },
        )

    def test_backoff_store_same_host_last_save_wins(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
        host_backoff_store.save({"a.org": {"learned_seconds": 10.0, "updated_at": 1.0}}, environ=env)
        host_backoff_store.save({"a.org": {"learned_seconds": 30.0, "updated_at": 2.0}}, environ=env)
        self.assertEqual(host_backoff_store.load(environ=env), {"a.org": {"learned_seconds": 30.0, "updated_at": 2.0}})

    def test_backoff_store_explicit_zero_deletes_only_that_host(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
        host_backoff_store.save(
            {
                "a.org": {"learned_seconds": 10.0, "updated_at": 1.0},
                "b.org": {"learned_seconds": 20.0, "updated_at": 2.0},
            },
            environ=env,
        )
        host_backoff_store.save({"a.org": {"learned_seconds": 0.0}}, environ=env)
        self.assertEqual(host_backoff_store.load(environ=env), {"b.org": {"learned_seconds": 20.0, "updated_at": 2.0}})

    def test_backoff_store_empty_and_malformed_mappings_fail_open(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
        original = {"a.org": {"learned_seconds": 10.0, "updated_at": 1.0}}
        host_backoff_store.save(original, environ=env)
        host_backoff_store.save({}, environ=env)
        host_backoff_store.save(
            {"bad.org": {"learned_seconds": "nan", "updated_at": 1.0}, "also-bad.org": {}},
            environ=env,
        )
        self.assertEqual(host_backoff_store.load(environ=env), original)

    def test_backoff_store_two_connections_preserve_disjoint_saves(self):
        from core.fetch.transport import host_backoff_store

        for _ in range(10):
            state_dir = tempfile.mkdtemp()
            self.addCleanup(lambda path=state_dir: shutil.rmtree(path, ignore_errors=True))
            env = {"CITATION_VERIFIER_STATE_DIR": state_dir}
            ready = threading.Barrier(3)
            errors = []

            def save_host(host, learned_seconds):
                try:
                    ready.wait()
                    host_backoff_store.save(
                        {host: {"learned_seconds": learned_seconds, "updated_at": learned_seconds}},
                        environ=env,
                    )
                except Exception as exc:
                    errors.append(exc)

            first = threading.Thread(target=save_host, args=("a.org", 10.0))
            second = threading.Thread(target=save_host, args=("b.org", 20.0))
            first.start()
            second.start()
            ready.wait()
            first.join()
            second.join()
            self.assertEqual(errors, [])
            self.assertEqual(
                host_backoff_store.load(environ=env),
                {
                    "a.org": {"learned_seconds": 10.0, "updated_at": 10.0},
                    "b.org": {"learned_seconds": 20.0, "updated_at": 20.0},
                },
            )

    def test_recovered_learning_exports_a_tombstone(self):
        limiter = host_limiter.HostLimiter(
            default_rate=host_limiter.HostRate.per_second(1000.0),
            wall_time_fn=lambda: 100.0,
        )
        limiter.load_learned({"a.org": {"learned_seconds": 10.0, "updated_at": 100.0}})
        for _ in range(4):
            limiter.report_success("a.org")
        self.assertEqual(
            limiter.export_learned(),
            {"a.org": {"learned_seconds": 0.0, "updated_at": 0.0}},
        )

    def test_explicit_host_cooldown_store_persists_across_isolated_paper_states(self):
        root = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        first_clock, second_clock = _FakeClock(), _FakeClock()
        first_clock.now = 100.0
        second_clock.now = 20.0
        wall = {"now": 1_000.0}
        shared_db = str(Path(root) / "suite" / "host-cooldowns.sqlite")
        first_env = {
            "CITATION_VERIFIER_STATE_DIR": str(Path(root) / "paper-one"),
            "CITATION_VERIFIER_HOST_COOLDOWN_DB": shared_db,
        }
        second_env = {
            "CITATION_VERIFIER_STATE_DIR": str(Path(root) / "paper-two"),
            "CITATION_VERIFIER_HOST_COOLDOWN_DB": shared_db,
        }
        rate = host_limiter.HostRate.per_second(1000.0)
        first = host_limiter.HostLimiter(
            rules={"example.org": rate}, time_fn=first_clock.monotonic,
            sleep_fn=first_clock.sleep, wall_time_fn=lambda: wall["now"], environ=first_env,
        )
        first.cooldown("api.example.org")
        wall["now"] = 1_001.0

        second = host_limiter.HostLimiter(
            rules={"example.org": rate}, time_fn=second_clock.monotonic,
            sleep_fn=second_clock.sleep, wall_time_fn=lambda: wall["now"], environ=second_env,
        )
        self.assertAlmostEqual(second.cooldown_until("api.example.org"), 24.0)
        with self.assertRaises(host_limiter.HostCooldownExceeded):
            second.acquire_for_fetch_url("https://api.example.org/fulltext")

        second_clock.now = 25.0
        wall["now"] = 1_006.0
        second.cooldown("api.example.org")
        self.assertAlmostEqual(second.snapshot()["api.example.org"]["last_applied_seconds"], 10.0)
        second.report_success("api.example.org")
        second.report_success("api.example.org")
        third = host_limiter.HostLimiter(
            rules={"example.org": rate}, time_fn=first_clock.monotonic,
            sleep_fn=first_clock.sleep, wall_time_fn=lambda: wall["now"], environ=first_env,
        )
        state = third.snapshot()["api.example.org"]
        self.assertFalse(state["in_cooldown"])
        self.assertAlmostEqual(state["learned_seconds"], 10.0)
        third.report_success("api.example.org")
        fourth = host_limiter.HostLimiter(
            rules={"example.org": rate}, time_fn=second_clock.monotonic,
            sleep_fn=second_clock.sleep, wall_time_fn=lambda: wall["now"], environ=second_env,
        )
        self.assertEqual(fourth.snapshot()["api.example.org"]["post_cooldown_successes"], 1)

    def test_malformed_explicit_host_cooldown_store_fails_before_transport(self):
        from core.fetch.transport import host_backoff_store
        from core.resolve import http as resolve_http

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        bad_path = str(Path(state_dir) / "missing" / "cooldowns.sqlite")
        Path(state_dir, "missing").write_text("not a directory", encoding="utf-8")
        calls = []
        with mock.patch.dict(os.environ, {"CITATION_VERIFIER_HOST_COOLDOWN_DB": bad_path}, clear=False), \
             mock.patch.object(resolve_http, "open_request", side_effect=lambda *args, **kwargs: calls.append(args)):
            host_limiter.reset_shared_limiter()
            with self.assertRaises(host_backoff_store.HostCooldownStoreError):
                resolve_http._get("https://api.openalex.org/works/x")
        self.assertEqual(calls, [])

    def test_malformed_persisted_host_cooldown_fails_before_transport(self):
        from core.fetch.transport import host_backoff_store
        from core.resolve import http as resolve_http

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "cooldowns.sqlite")
        host_backoff_store.save_cooldown(
            "api.example.org",
            {
                "cooldown_until": 1.0,
                "last_applied_seconds": 1.0,
                "in_cooldown": True,
                "post_cooldown_successes": 0,
                "learned_seconds": 0.0,
                "learned_updated_at": 0.0,
            },
            environ={"CITATION_VERIFIER_HOST_COOLDOWN_DB": db_path},
        )
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE host_cooldown SET in_cooldown = 2")
        calls = []
        with mock.patch.dict(os.environ, {"CITATION_VERIFIER_HOST_COOLDOWN_DB": db_path}, clear=False), \
             mock.patch.object(resolve_http, "open_request", side_effect=lambda *args, **kwargs: calls.append(args)):
            host_limiter.reset_shared_limiter()
            with self.assertRaises(host_backoff_store.HostCooldownStoreError):
                resolve_http._get("https://api.openalex.org/works/x")
        self.assertEqual(calls, [])

    def test_generic_store_does_not_govern_semantic_scholar(self):
        from core.fetch.transport import host_backoff_store

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        generic_db = str(Path(state_dir) / "host-cooldowns.sqlite")
        semantic_db = str(Path(state_dir) / "semantic-scholar.sqlite")
        env = {
            "CITATION_VERIFIER_HOST_COOLDOWN_DB": generic_db,
            "CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": semantic_db,
        }
        host_backoff_store.save_cooldown(
            "api.semanticscholar.org",
            {
                "cooldown_until": 1_000.0,
                "last_applied_seconds": 30.0,
                "in_cooldown": True,
                "post_cooldown_successes": 0,
                "learned_seconds": 0.0,
                "learned_updated_at": 0.0,
            },
            environ=env,
        )
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000.0)},
            wall_time_fn=lambda: 0.0,
            environ=env,
        )
        self.assertEqual(limiter.acquire_for_fetch_url("https://api.semanticscholar.org/graph/v1/paper"), 1)

    def test_success_without_an_open_cooldown_is_a_noop(self):
        limiter = host_limiter.HostLimiter(
            default_rate=host_limiter.HostRate.per_second(1000.0),
        )
        limiter.acquire("api.s2.org")
        limiter.report_success("api.s2.org")  # nothing to learn, must not raise
        self.assertEqual(
            limiter.snapshot().get("api.s2.org", {}).get("learned_seconds", 0.0), 0.0)

    def test_explicit_semantic_scholar_pacing_shares_admission_slots(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "semantic-scholar.sqlite")
        first_clock, second_clock = _FakeClock(), _FakeClock()
        first_state, second_state = str(Path(state_dir) / "first"), str(Path(state_dir) / "second")
        first_env = {
            "CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path,
            "CITATION_VERIFIER_STATE_DIR": first_state,
        }
        second_env = {
            "CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path,
            "CITATION_VERIFIER_STATE_DIR": second_state,
        }
        config = provider_config.load(environ={})
        semantic_config = config["providers"]["semantic_scholar"]
        production_rate = host_limiter._rate_from_config(
            semantic_config["rate"], has_api_key=False,
        )
        self.assertIsNotNone(production_rate)
        first = host_limiter.HostLimiter(
            rules={"semanticscholar.org": production_rate},
            time_fn=first_clock.monotonic, sleep_fn=first_clock.sleep,
            wall_time_fn=first_clock.monotonic, environ=first_env,
        )
        second = host_limiter.HostLimiter(
            rules={"semanticscholar.org": production_rate},
            time_fn=second_clock.monotonic, sleep_fn=second_clock.sleep,
            wall_time_fn=second_clock.monotonic, environ=second_env,
        )

        self.assertEqual(first.acquire("api.semanticscholar.org"), 1)
        self.assertEqual(second.acquire("api.semanticscholar.org"), 2)
        self.assertEqual(second_clock.sleeps, [2.0])

    def test_explicit_semantic_scholar_pacing_persists_cooldown_recovery(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "semantic-scholar.sqlite")
        clock = _FakeClock()
        env = {"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path}
        first = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep, wall_time_fn=clock.monotonic, environ=env,
        )
        token = first.acquire("api.semanticscholar.org")
        first.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        second = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep, wall_time_fn=clock.monotonic, environ=env,
        )
        with self.assertRaises(host_limiter.HostCooldownExceeded):
            second.acquire("api.semanticscholar.org", max_wait_seconds=4.0)
        second.cooldown("api.semanticscholar.org", 0.0, admission_token=token + 1)
        self.assertEqual(second._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 10.0)
        second.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        self.assertEqual(second._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 10.0)
        second.report_success("api.semanticscholar.org")
        second.report_success("api.semanticscholar.org")
        self.assertEqual(second._semantic_scholar_pacer.snapshot()["learned_seconds"], 10.0)

    def test_explicit_semantic_scholar_pacing_late_429_does_not_restart_cooldown(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "semantic-scholar.sqlite")
        clock = _FakeClock()
        env = {"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path}
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic, environ=env,
        )

        first = limiter.acquire("api.semanticscholar.org")
        concurrent = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=first)
        initial_cooldown = limiter.cooldown_until("api.semanticscholar.org")

        clock.now = initial_cooldown + 24 * 3600.0
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=concurrent)
        self.assertEqual(limiter.cooldown_until("api.semanticscholar.org"), initial_cooldown)

    def test_explicit_semantic_scholar_pacing_slow_retry_response_still_doubles(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic,
            environ={"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": str(
                Path(state_dir) / "semantic-scholar.sqlite"
            )},
        )

        limiter.cooldown("api.semanticscholar.org", 0.0)
        clock.now = 5.0
        token = limiter.acquire("api.semanticscholar.org")

        # The retry was admitted when the first wait expired.  A slow 429
        # response must still count as consecutive, rather than as inactivity.
        clock.now = 10.0
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 10.0)

    def test_explicit_semantic_scholar_pacing_stale_successes_do_not_relearn_raw_wait(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic,
            environ={"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": str(
                Path(state_dir) / "semantic-scholar.sqlite"
            )},
        )

        for _ in range(7):
            limiter.cooldown("api.semanticscholar.org", 0.0)
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 300.0)

        clock.now = 300.0 + 14 * 3600.0
        stale_decay = 300.0 * (0.5 ** (14.0 / 24.0))
        limiter.acquire("api.semanticscholar.org")
        limiter.report_success("api.semanticscholar.org")
        limiter.acquire("api.semanticscholar.org")
        limiter.report_success("api.semanticscholar.org")

        # Two clean responses adapt the decayed value.  They must not promote
        # the raw 300-second stale episode back into current learning.
        token = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        self.assertAlmostEqual(
            limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"],
            stale_decay * 0.5,
            places=5,
        )

    def test_explicit_semantic_scholar_pacing_decays_a_stale_open_episode(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "semantic-scholar.sqlite")
        clock = _FakeClock()
        env = {"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path}
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic, environ=env,
        )

        for _ in range(7):
            limiter.cooldown("api.semanticscholar.org", 0.0)
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 300.0)

        # Reproduce the inherited canary state: an unresolved 300-second
        # episode has been idle for fourteen hours after its deadline.
        clock.now = 300.0 + 14 * 3600.0
        token = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        expected = 300.0 * (0.5 ** (14.0 / 24.0))
        self.assertAlmostEqual(
            limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], expected,
        )

        # A fresh token at the new deadline remains in the current episode and
        # therefore escalates normally.
        clock.now += expected
        token = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 300.0)

    def test_explicit_semantic_scholar_pacing_short_expiry_retry_still_doubles(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic,
            environ={"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": str(
                Path(state_dir) / "semantic-scholar.sqlite"
            )},
        )

        limiter.cooldown("api.semanticscholar.org", 0.0)
        clock.now = 5.1
        token = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 0.0, admission_token=token)
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["last_applied_seconds"], 10.0)

    def test_explicit_semantic_scholar_pacing_decays_stale_learning(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        db_path = str(Path(state_dir) / "semantic-scholar.sqlite")
        clock = _FakeClock()
        clock.now = 100.0
        env = {"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": db_path}
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1000)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic, environ=env,
        )
        limiter.cooldown("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org")
        limiter.report_success("api.semanticscholar.org")
        limiter.report_success("api.semanticscholar.org")
        self.assertEqual(limiter._semantic_scholar_pacer.snapshot()["learned_seconds"], 20.0)

        clock.now += 24 * 3600.0
        limiter.cooldown("api.semanticscholar.org")
        snapshot = limiter._semantic_scholar_pacer.snapshot()
        self.assertAlmostEqual(snapshot["learned_seconds"], 10.0)
        self.assertAlmostEqual(snapshot["last_applied_seconds"], 10.0)

    def test_explicit_pacing_is_opt_in_and_only_semantic_scholar(self):
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"openalex.org": host_limiter.HostRate.per_second(1)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep, wall_time_fn=clock.monotonic,
        )
        limiter.acquire("api.openalex.org")
        limiter.acquire("api.openalex.org")
        self.assertEqual(clock.sleeps, [1.0])

    def test_explicit_semantic_scholar_pacing_preserves_unlimited_rate(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(0)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep,
            wall_time_fn=clock.monotonic,
            environ={
                "CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": str(
                    Path(state_dir) / "semantic-scholar.sqlite"
                )
            },
        )

        limiter.acquire("api.semanticscholar.org")
        limiter.acquire("api.semanticscholar.org")
        self.assertEqual(clock.sleeps, [])

    def test_fetch_admission_declines_shared_semantic_scholar_cooldown_without_slot(self):
        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        clock = _FakeClock()
        limiter = host_limiter.HostLimiter(
            rules={"semanticscholar.org": host_limiter.HostRate.per_second(1)},
            time_fn=clock.monotonic, sleep_fn=clock.sleep, wall_time_fn=clock.monotonic,
            environ={"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": str(
                Path(state_dir) / "semantic-scholar.sqlite"
            )},
        )
        token = limiter.acquire("api.semanticscholar.org")
        limiter.cooldown("api.semanticscholar.org", 30, admission_token=token)
        before = limiter._semantic_scholar_pacer.snapshot()
        with self.assertRaises(host_limiter.HostCooldownExceeded):
            limiter.acquire_for_fetch_url("https://api.semanticscholar.org/graph/v1/paper")
        after = limiter._semantic_scholar_pacer.snapshot()
        self.assertEqual(after["last_admission_token"], before["last_admission_token"])
        self.assertEqual(after["next_allowed"], before["next_allowed"])

    def test_malformed_explicit_pacing_fails_before_transport(self):
        from core.resolve import http as resolve_http

        state_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(state_dir, ignore_errors=True))
        bad_path = str(Path(state_dir) / "missing" / "pacing.sqlite")
        Path(state_dir, "missing").write_text("not a directory", encoding="utf-8")
        calls = []
        with mock.patch.dict(os.environ, {"CITATION_VERIFIER_SEMANTIC_SCHOLAR_PACING_DB": bad_path}, clear=False), \
             mock.patch.object(resolve_http, "open_request", side_effect=lambda *args, **kwargs: calls.append(args)):
            host_limiter.reset_shared_limiter()
            with self.assertRaises(Exception):
                resolve_http._get("https://api.semanticscholar.org/graph/v1/paper/x")
        self.assertEqual(calls, [])

    def _write_config(self, payload: dict) -> str:
        handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False)
        try:
            json.dump(payload, handle, indent=2)
        finally:
            handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name


if __name__ == "__main__":
    unittest.main()
