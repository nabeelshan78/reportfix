import time

import pytest

from app.triage import llm
from app.triage.llm import BadKeyError, ProviderError, QuotaError
from tests.fakes import FakeProvider, make_ext, use_chain


class Clock:
    """Controllable clock so cooldown / rate-guard tests never sleep."""

    def __init__(self, now=1_700_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def run(chain, text="lab 3 wifi down"):
    return chain.triage(text, text)


# ---- chain order ----
def test_chain_uses_first_provider_when_it_works(tmp_path):
    a, b = FakeProvider("a", "model-a", make_ext(summary_en="from a")), FakeProvider("b", "model-b")
    chain = use_chain(a, b, cache_path=str(tmp_path / "c.db"))
    res = run(chain)
    assert res.engine == "model-a" and res.extraction.summary_en == "from a"
    assert len(a.calls) == 1 and b.calls == []


def test_invalid_output_falls_through_to_next_provider(tmp_path):
    a = FakeProvider("a", "model-a", errors=[ProviderError("invalid JSON from the model")])
    b = FakeProvider("b", "model-b", make_ext(summary_en="from b"))
    res = run(use_chain(a, b, cache_path=str(tmp_path / "c.db")))
    assert res.engine == "model-b"
    assert any("model-a failed" in n for n in res.notes)


def test_all_providers_failing_gives_rules_result_never_an_exception(tmp_path):
    a = FakeProvider("a", "model-a", errors=[ProviderError("HTTP 503")])
    b = FakeProvider("b", "model-b", errors=[RuntimeError("boom")])
    res = run(use_chain(a, b, cache_path=str(tmp_path / "c.db")), "lab 3 wifi not working")
    assert res.engine == "rules" and res.rules_mode
    assert res.extraction.problem_type == "wifi_internet"


def test_no_providers_is_rules_mode(tmp_path):
    res = run(use_chain(cache_path=str(tmp_path / "c.db")), "lab 3 wifi not working")
    assert res.engine == "rules" and res.notes == []


def test_hard_timeout_moves_to_next_provider(tmp_path):
    class Slow(FakeProvider):
        def call(self, key_index, system, user):
            time.sleep(0.6)
            return make_ext()

    b = FakeProvider("b", "model-b")
    res = run(use_chain(Slow("a", "model-a"), b, cache_path=str(tmp_path / "c.db"), timeout=0.1))
    assert res.engine == "model-b" and any("timed out" in n for n in res.notes)


# ---- Q2: 429 -> cooldown, next provider answers, NO retry ----
def test_q2_429_cools_down_model_and_next_provider_answers_without_retry(tmp_path):
    clock = Clock()
    a = FakeProvider("a", "model-a", errors=[QuotaError(30)])
    b = FakeProvider("b", "model-b", make_ext(summary_en="from b"))
    chain = use_chain(a, b, clock=clock, cache_path=str(tmp_path / "c.db"))

    res = run(chain, "first report")
    assert res.engine == "model-b"
    assert len(a.calls) == 1, "a 429 must never be retried"
    assert any("Q2" in n and "model-a" in n for n in res.notes)
    assert "cooling down" in chain.status()["providers"][0]["state"]

    run(chain, "second different report")  # model-a is skipped without being called
    assert len(a.calls) == 1 and len(b.calls) == 2

    clock.now += 40  # cooldown (30 s + 1) is over
    run(chain, "third different report")
    assert len(a.calls) == 2


def test_q2_daily_quota_cools_down_until_next_pacific_midnight(tmp_path):
    clock = Clock()
    a = FakeProvider("a", "model-a", errors=[QuotaError(None, per_day=True)])
    chain = use_chain(a, FakeProvider("b", "model-b"), clock=clock,
                      cache_path=str(tmp_path / "c.db"))
    run(chain)
    st = chain.state[("a", 0)]
    assert st.cooldown_until == llm.next_pacific_midnight(clock.now)
    assert st.cooldown_until > clock.now + 60


def test_q2_default_cooldown_is_60_seconds_when_no_delay_given(tmp_path):
    clock = Clock()
    a = FakeProvider("a", "model-a", errors=[QuotaError(None, False)])
    chain = use_chain(a, FakeProvider("b", "model-b"), clock=clock,
                      cache_path=str(tmp_path / "c.db"))
    run(chain)
    assert chain.state[("a", 0)].cooldown_until == pytest.approx(clock.now + 60)


def test_quota_info_parses_retry_delay_and_per_day():
    assert llm.quota_info(Exception("Quota exceeded. Please retry in 33.5s.")) == (33.5, False)
    delay, per_day = llm.quota_info(Exception("GenerateRequestsPerDayPerProjectPerModel exceeded"))
    assert per_day is True and delay is None


def test_gemini_errors_are_translated():
    class Err(Exception):
        def __init__(self, code, msg=""):
            super().__init__(msg)
            self.code = code

    conv = llm.GeminiProvider._convert
    assert isinstance(conv(Err(429, "retry in 5s")), QuotaError)
    assert isinstance(conv(Err(403)), BadKeyError)
    assert isinstance(conv(Err(400, "API key not valid")), BadKeyError)
    assert isinstance(conv(Err(500)), ProviderError)
    assert isinstance(conv(Err(404)), ProviderError)


# ---- two Gemini keys: when one is out of quota, the other is tried ----
def test_second_key_is_used_when_first_key_hits_quota(tmp_path):
    a = FakeProvider("a", "model-a", errors=[QuotaError(30)], n_keys=2)
    b = FakeProvider("b", "model-b")
    res = run(use_chain(a, b, cache_path=str(tmp_path / "c.db")))
    assert a.calls == [0, 1] and res.engine == "model-a" and b.calls == []


def test_bad_key_is_skipped_and_next_key_tried(tmp_path):
    a = FakeProvider("a", "model-a", errors=[BadKeyError("rejected")], n_keys=2)
    chain = use_chain(a, cache_path=str(tmp_path / "c.db"))
    res = run(chain)
    assert a.calls == [0, 1] and res.engine == "model-a"
    assert chain.state[("a", 0)].cooldown_until > chain.clock()


# ---- Q1: rate guard skips a model at its per-minute limit WITHOUT calling it ----
def test_q1_rate_guard_skips_model_at_limit_without_calling(tmp_path):
    clock = Clock()
    a = FakeProvider("a", "model-a", rpm_limit=2)
    b = FakeProvider("b", "model-b")
    chain = use_chain(a, b, clock=clock, cache_path=str(tmp_path / "c.db"))
    run(chain, "report one")
    run(chain, "report two")
    res = run(chain, "report three")
    assert len(a.calls) == 2, "model-a must not be called past its limit"
    assert res.engine == "model-b" and any("Q1" in n for n in res.notes)

    clock.now += 61  # the sliding window has moved on
    run(chain, "report four")
    assert len(a.calls) == 3


def test_q1_when_every_provider_is_guarded_the_rules_engine_answers(tmp_path):
    clock = Clock()
    a = FakeProvider("a", "model-a", rpm_limit=1)
    chain = use_chain(a, clock=clock, cache_path=str(tmp_path / "c.db"))
    run(chain, "one")
    res = run(chain, "two lab 3 wifi")
    assert res.engine == "rules" and len(a.calls) == 1
    assert chain.status()["providers"][0]["state"] == "rate-guarded"


# ---- Q4: cache ----
def test_q4_cache_hit_makes_no_provider_call(tmp_path):
    a = FakeProvider("a", "model-a")
    chain = use_chain(a, cache_path=str(tmp_path / "c.db"))
    first = run(chain, "same sentence")
    second = run(chain, "same sentence")
    assert len(a.calls) == 1
    assert second.cached and not first.cached and second.latency_ms == 0


def test_q4_cache_survives_a_restart(tmp_path):
    path = str(tmp_path / "c.db")
    run(use_chain(FakeProvider("a", "model-a"), cache_path=path), "restart sentence")
    a2 = FakeProvider("a", "model-a")
    res = run(use_chain(a2, cache_path=path), "restart sentence")  # a brand-new chain
    assert res.cached and a2.calls == []


def test_q4_rules_results_are_not_cached(tmp_path):
    chain = use_chain(FakeProvider("a", "model-a", errors=[ProviderError("x")]),
                      cache_path=str(tmp_path / "c.db"))
    first = run(chain, "lab 3 wifi not working")
    assert first.engine == "rules"
    again = run(chain, "lab 3 wifi not working")  # provider works now, so it is called
    assert again.engine == "model-a" and not again.cached


def test_prompt_version_is_part_of_the_cache_key(monkeypatch):
    k1 = llm.cache_key("hello")
    monkeypatch.setattr(llm, "PROMPT_VERSION", "v999")
    assert llm.cache_key("hello") != k1


# ---- shared helper for grouping tie-breaks / summary ----
def test_ask_text_goes_through_the_same_chain_and_cache(tmp_path):
    a = FakeProvider("a", "model-a", text="YES")
    chain = use_chain(a, cache_path=str(tmp_path / "c.db"))
    assert chain.ask_text("same problem?") == "YES"
    assert chain.ask_text("same problem?") == "YES"
    assert len(a.calls) == 1


def test_ask_text_returns_none_when_every_provider_fails(tmp_path):
    chain = use_chain(FakeProvider("a", "model-a", errors=[ProviderError("x")]),
                      cache_path=str(tmp_path / "c.db"))
    assert chain.ask_text("anything") is None


# ---- health ----
def test_status_reports_per_provider_details(tmp_path):
    chain = use_chain(FakeProvider("a", "model-a"), cache_path=str(tmp_path / "c.db"))
    assert chain.status()["providers"][0]["state"] == "ready"
    run(chain)
    s = chain.status()
    p = s["providers"][0]
    assert p["state"] == "ok" and p["calls_last_minute"] == 1 and p["calls_today"] == 1
    assert p["last_success"] and s["last_engine"] == "model-a" and s["mode"] == "ai"
    assert s["providers"][-1]["id"] == "rules"


def test_default_chain_without_keys_is_rules_mode(monkeypatch):
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_2", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "")
    chain = llm.build_default_chain()
    assert chain.status()["mode"] == "rules"
    res = chain.triage("lab 3 wifi not working", "lab 3 wifi not working")
    assert res.engine == "rules"
