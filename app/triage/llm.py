"""AI provider chain (CLAUDE.md section 7, Providers).

Order follows LLM_ORDER: gemini -> gemini2 -> claude (only if a key is set) -> rules engine.
  Q1  rate guard: a model/key that already made GEMINI_RPM_LIMIT calls in the last 60 s is
      skipped WITHOUT calling it. We never sleep inside a request.
  Q2  quota cooldown: on a 429 we cool that model/key down and move on, no retry.
  Q3  one AI call per report (the pipeline reuses the stored extraction for clarification).
  Q4  cache: valid AI answers live in llm_cache.db (separate file, never reset).
Gemini keys: GEMINI_API_KEY and GEMINI_API_KEY_2 are separate free quotas. When one key is
out of quota the chain tries the other key before moving to the next model.
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from app import campus
from app.triage.prompts import PROMPT_VERSION, build_user_message, system_prompt
from app.triage.schema import TriageExtraction

ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(ROOT / ".env")

_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm")


# ----------------------------------------------------------------------
# Errors (providers translate SDK errors into these)
# ----------------------------------------------------------------------
class QuotaError(Exception):
    """429 / RESOURCE_EXHAUSTED."""

    def __init__(self, retry_seconds: float | None = None, per_day: bool = False):
        super().__init__("quota exhausted")
        self.retry_seconds = retry_seconds
        self.per_day = per_day


class BadKeyError(Exception):
    """The key itself was rejected (invalid / no permission)."""


class ProviderError(Exception):
    """Timeout, 5xx, invalid output, model not found..."""


def quota_info(exc: Exception) -> tuple[float | None, bool]:
    """Read the retry delay and whether the quota is per-day from a 429 error."""
    text = str(exc)
    low = text.lower()
    delay = None
    m = re.search(r"retry in ([\d.]+)\s*s", low) or \
        re.search(r"retrydelay['\"]?\s*[:=]\s*['\"]?([\d.]+)s", low)
    if m:
        delay = float(m.group(1))
    per_day = "perday" in low.replace(" ", "").replace("_", "") or "per day" in low \
        or "daily" in low
    return delay, per_day


def next_pacific_midnight(now_ts: float) -> float:
    """Free-tier daily quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:  # tzdata missing: fixed UTC-8
        tz = timezone(timedelta(hours=-8))
    local = datetime.fromtimestamp(now_ts, tz)
    nxt = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------
def gemini_keys() -> list[str]:
    names = ("GEMINI_API_KEY", "GEMINI_API_KEY_2")
    return [k for k in (os.getenv(n, "").strip() for n in names) if k]


def _timeout_seconds() -> float:
    try:
        return float(os.getenv("LLM_TIMEOUT_SECONDS", "12") or 12)
    except ValueError:
        return 12.0


class Provider:
    """Base class. Tests use FakeProvider subclasses of this."""
    id = "base"
    label = "Base"
    model = ""
    n_keys = 1
    rpm_limit = 8

    def configured(self) -> bool:
        return True

    def call(self, key_index: int, system: str, user: str) -> TriageExtraction:
        raise NotImplementedError

    def call_text(self, key_index: int, prompt: str) -> str:
        raise NotImplementedError


class GeminiProvider(Provider):
    def __init__(self, pid: str, label: str, model_env: str):
        self.id, self.label, self.model_env = pid, label, model_env
        self._clients: dict[str, object] = {}
        self._think = True  # lowest thinking level; dropped if the model rejects it

    @property
    def model(self) -> str:  # re-read so editing .env needs no code change
        return os.getenv(self.model_env, "").strip()

    @property
    def n_keys(self) -> int:
        return len(gemini_keys())

    @property
    def rpm_limit(self) -> int:
        try:
            return int(os.getenv("GEMINI_RPM_LIMIT", "8") or 8)
        except ValueError:
            return 8

    def configured(self) -> bool:
        return bool(gemini_keys() and self.model)

    def _client(self, key_index: int):
        from google import genai
        from google.genai import types
        key = gemini_keys()[key_index]
        if key not in self._clients:
            self._clients[key] = genai.Client(api_key=key, http_options=types.HttpOptions(
                timeout=int(_timeout_seconds() * 1000),
                retry_options=types.HttpRetryOptions(attempts=1)))
        return self._clients[key]

    def _generate(self, key_index: int, system: str, contents: str, schema: bool, json_mode: bool,
                  max_tokens: int):
        from google.genai import types
        kwargs = dict(system_instruction=system, temperature=0, max_output_tokens=max_tokens)
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        if schema:
            kwargs["response_schema"] = TriageExtraction
        if self._think:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel.MINIMAL)
        client = self._client(key_index)
        try:
            return client.models.generate_content(
                model=self.model, contents=contents,
                config=types.GenerateContentConfig(**kwargs))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if self._think and code == 400 and "think" in str(exc).lower():
                self._think = False  # this model has no thinking setting: drop it
                return self._generate(key_index, system, contents, schema, json_mode, max_tokens)
            raise

    @staticmethod
    def _convert(exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        msg = str(exc)
        if code == 429:
            delay, per_day = quota_info(exc)
            return QuotaError(delay, per_day)
        if code in (401, 403) or (code == 400 and "api key" in msg.lower()):
            return BadKeyError(f"key rejected (HTTP {code})")
        if code == 404:
            return ProviderError(f"model not found (HTTP 404)")
        if code is not None:
            return ProviderError(f"HTTP {code}")
        return ProviderError(type(exc).__name__)

    def call(self, key_index, system, user):
        try:
            try:
                resp = self._generate(key_index, system, user, True, True, 800)
            except (ValueError, TypeError) as exc:
                resp = self._schema_fallback(key_index, system, user, exc)
            except Exception as exc:
                if getattr(exc, "code", None) == 400 and "schema" in str(exc).lower():
                    resp = self._schema_fallback(key_index, system, user, exc)
                else:
                    raise
        except (QuotaError, BadKeyError, ProviderError):
            raise
        except Exception as exc:
            raise self._convert(exc) from None
        return _parse_response(resp)

    def _schema_fallback(self, key_index, system, user, _exc):
        """response_schema was refused: retry ONCE with JSON mime type + schema in the prompt."""
        spelled = system + "\n\nReturn ONLY JSON matching this schema:\n" + \
            json.dumps(TriageExtraction.model_json_schema())
        return self._generate(key_index, spelled, user, False, True, 800)

    def call_text(self, key_index, prompt):
        try:
            resp = self._generate(key_index, "You are a concise assistant.", prompt,
                                  False, False, 400)
        except Exception as exc:
            raise self._convert(exc) from None
        text = (getattr(resp, "text", None) or "").strip()
        if not text:
            raise ProviderError("empty answer")
        return text


def _parse_response(resp) -> TriageExtraction:
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, TriageExtraction):
        return parsed
    text = getattr(resp, "text", None)
    if not text:
        raise ProviderError("empty answer")
    try:
        return TriageExtraction.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        raise ProviderError("invalid JSON from the model") from None


class ClaudeProvider(Provider):
    id, label = "claude", "Claude"

    @property
    def model(self) -> str:
        return os.getenv("CLAUDE_MODEL", "").strip()

    def configured(self) -> bool:
        return bool(os.getenv("ANTHROPIC_API_KEY", "").strip() and self.model)

    def _client(self):
        import anthropic
        return anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
                                   timeout=_timeout_seconds(), max_retries=0)

    @staticmethod
    def _convert(exc: Exception) -> Exception:
        import anthropic
        if isinstance(exc, anthropic.RateLimitError):
            return QuotaError(None, False)
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return BadKeyError("key rejected")
        return ProviderError(type(exc).__name__)

    def call(self, key_index, system, user):
        tool = {"name": "record_triage", "description": "Record the structured triage facts.",
                "input_schema": TriageExtraction.model_json_schema()}
        try:
            resp = self._client().messages.create(
                model=self.model, max_tokens=800, temperature=0, system=system,
                tools=[tool], tool_choice={"type": "tool", "name": "record_triage"},
                messages=[{"role": "user", "content": user}])
        except Exception as exc:
            raise self._convert(exc) from None
        for block in resp.content:
            if getattr(block, "type", "") == "tool_use":
                try:
                    return TriageExtraction.model_validate(block.input)
                except ValidationError:
                    raise ProviderError("invalid tool output") from None
        raise ProviderError("no tool_use block")

    def call_text(self, key_index, prompt):
        try:
            resp = self._client().messages.create(
                model=self.model, max_tokens=400, temperature=0,
                messages=[{"role": "user", "content": prompt}])
        except Exception as exc:
            raise self._convert(exc) from None
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        if not text:
            raise ProviderError("empty answer")
        return text


# ----------------------------------------------------------------------
# Cache (Q4): separate SQLite file, keyed by sha256(PROMPT_VERSION + text)
# ----------------------------------------------------------------------
class Cache:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("LLM_CACHE_DB") or str(ROOT / "llm_cache.db")
        self._lock = threading.Lock()
        self._ready = False

    def _conn(self):
        con = sqlite3.connect(self.path, timeout=5)
        if not self._ready:
            con.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, "
                        "value TEXT NOT NULL, engine TEXT NOT NULL, created REAL NOT NULL)")
            self._ready = True
        return con

    def get(self, key: str) -> tuple[str, str] | None:
        try:
            with self._lock, self._conn() as con:
                row = con.execute("SELECT value, engine FROM cache WHERE key=?", (key,)).fetchone()
            return (row[0], row[1]) if row else None
        except sqlite3.Error:
            return None  # the cache is an optimisation, never a failure

    def put(self, key: str, value: str, engine: str) -> None:
        try:
            with self._lock, self._conn() as con:
                con.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                            (key, value, engine, time.time()))
        except sqlite3.Error:
            pass


def cache_key(text: str, kind: str = "triage") -> str:
    return hashlib.sha256(
        f"{kind}:{PROMPT_VERSION}:{campus.get().hash}:{text}".encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------
# Chain
# ----------------------------------------------------------------------
@dataclass
class KeyState:
    calls: deque = field(default_factory=deque)
    cooldown_until: float = 0.0
    last_success: float | None = None
    last_failure: float | None = None
    day: str = ""
    calls_today: int = 0


@dataclass
class LLMResult:
    extraction: TriageExtraction
    engine: str  # model name, or "rules"
    latency_ms: int
    cached: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def rules_mode(self) -> bool:
        return self.engine == "rules"


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M UTC")


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None


class Chain:
    def __init__(self, providers: list[Provider], cache: Cache | None = None,
                 clock=time.time, timeout: float | None = None,
                 all_providers: list[Provider] | None = None):
        self.providers = providers
        self.all_providers = all_providers or providers
        self.cache = cache or Cache()
        self.clock = clock
        self.timeout = timeout
        self.state: dict[tuple[str, int], KeyState] = {}
        self.last_engine: str | None = None
        self._lock = threading.Lock()

    # ---- state helpers ----
    def _st(self, p: Provider, ki: int) -> KeyState:
        return self.state.setdefault((p.id, ki), KeyState())

    def _recent(self, st: KeyState, now: float) -> int:
        while st.calls and now - st.calls[0] >= 60:
            st.calls.popleft()
        return len(st.calls)

    def _record_call(self, st: KeyState, now: float) -> None:
        st.calls.append(now)
        day = datetime.fromtimestamp(now, timezone(timedelta(hours=-8))).strftime("%Y-%m-%d")
        if st.day != day:
            st.day, st.calls_today = day, 0
        st.calls_today += 1

    def _run_with_timeout(self, fn):
        fut = _POOL.submit(fn)
        try:
            return fut.result(timeout=self.timeout or _timeout_seconds())
        except FutureTimeout:
            raise ProviderError("timed out") from None

    # ---- the walk: one attempt per model/key, never retries, never sleeps ----
    def _walk(self, invoke):
        """invoke(provider, key_index) -> value. Returns (value, provider, ms, notes)."""
        notes: list[str] = []
        for p in self.providers:
            if not p.configured():
                continue
            for ki in range(max(p.n_keys, 1)):
                st = self._st(p, ki)
                now = self.clock()
                if st.cooldown_until > now:
                    notes.append(f"Q2: {p.model} cooling down until {_hhmm(st.cooldown_until)}, skipped")
                    continue
                if self._recent(st, now) >= p.rpm_limit:
                    notes.append(f"Q1: {p.model} rate-guarded ({p.rpm_limit} calls/min), skipped")
                    continue
                self._record_call(st, now)
                t0 = time.perf_counter()
                try:
                    value = self._run_with_timeout(lambda p=p, ki=ki: invoke(p, ki))
                except QuotaError as e:
                    secs = e.retry_seconds
                    until = next_pacific_midnight(now) if e.per_day else now + (secs + 1 if secs else 60)
                    st.cooldown_until, st.last_failure = until, now
                    kind = "daily quota" if e.per_day else "quota"
                    notes.append(f"Q2: {p.model} {kind} exhausted, cooling down until "
                                 f"{_hhmm(until)}; trying the next option")
                    continue
                except BadKeyError:
                    st.cooldown_until, st.last_failure = now + 600, now
                    notes.append(f"{p.model}: API key rejected, skipped for 10 minutes")
                    continue
                except Exception as e:  # timeout, 5xx, invalid output...: next provider
                    st.last_failure = now
                    notes.append(f"{p.model} failed ({e}); trying the next provider")
                    break
                st.last_success = now
                self.last_engine = p.model
                return value, p, int((time.perf_counter() - t0) * 1000), notes
        return None, None, 0, notes

    # ---- public API ----
    def triage(self, cleaned: str, stripped: str) -> LLMResult:
        """One AI call per report (Q3). Falls back to the rules engine, never raises."""
        from app.triage import rules
        key = cache_key(cleaned)
        hit = self.cache.get(key)
        if hit:
            try:
                ext = TriageExtraction.model_validate(json.loads(hit[0]))
                self.last_engine = hit[1]
                return LLMResult(ext, hit[1], 0, cached=True)
            except (ValueError, ValidationError):
                pass
        user = build_user_message(cleaned)
        value, provider, ms, notes = self._walk(lambda p, ki: p.call(ki, system_prompt(), user))
        if value is not None:
            self.cache.put(key, value.model_dump_json(), provider.model)
            return LLMResult(value, provider.model, ms, notes=notes)
        t0 = time.perf_counter()
        ext = rules.rules_extract(cleaned, stripped)
        self.last_engine = "rules"
        return LLMResult(ext, "rules", int((time.perf_counter() - t0) * 1000), notes=notes)

    def ask_text(self, prompt: str) -> str | None:
        """Short text answer through the same chain (grouping tie-break, admin summary)."""
        key = cache_key(prompt, "text")
        hit = self.cache.get(key)
        if hit:
            return hit[0]
        value, provider, _ms, _notes = self._walk(lambda p, ki: p.call_text(ki, prompt))
        if value is None:
            return None
        self.cache.put(key, value, provider.model)
        return value

    def status(self) -> dict:
        now = self.clock()
        used = {p.id for p in self.providers}
        out = []
        for p in self.all_providers:
            keys = max(p.n_keys, 1) if p.configured() else 0
            states = [self._st(p, ki) for ki in range(keys)]
            if not p.configured():
                state = "not configured"
            elif p.id not in used:
                state = "disabled (not in LLM_ORDER)"
            elif states and all(s.cooldown_until > now for s in states):
                state = f"cooling down until {_hhmm(max(s.cooldown_until for s in states))}"
            elif states and all(self._recent(s, now) >= p.rpm_limit for s in states):
                state = "rate-guarded"
            elif any(s.last_success for s in states):
                state = "ok"
            else:
                state = "ready"
            fails = [s.last_failure for s in states if s.last_failure]
            wins = [s.last_success for s in states if s.last_success]
            out.append({
                "id": p.id, "label": p.label, "configured": p.configured(), "model": p.model,
                "keys": keys, "state": state,
                "last_success": _iso(max(wins)) if wins else None,
                "last_failure": _iso(max(fails)) if fails else None,
                "calls_last_minute": sum(self._recent(s, now) for s in states),
                "calls_today": sum(s.calls_today for s in states),
            })
        out.append({"id": "rules", "label": "Rules", "configured": True,
                    "model": "keyword + location rules", "keys": 0, "state": "ready",
                    "last_success": None, "last_failure": None,
                    "calls_last_minute": 0, "calls_today": 0})
        any_ai = any(p["configured"] and not p["state"].startswith("disabled")
                     for p in out[:-1])
        return {"providers": out, "last_engine": self.last_engine,
                "mode": "ai" if any_ai else "rules"}


# ----------------------------------------------------------------------
# Default chain + module-level helpers
# ----------------------------------------------------------------------
def build_default_chain() -> Chain:
    everything = {
        "gemini": GeminiProvider("gemini", "Gemini", "GEMINI_MODEL"),
        "gemini2": GeminiProvider("gemini2", "Gemini 2", "GEMINI_MODEL_2"),
        "claude": ClaudeProvider(),
    }
    order = [x.strip() for x in os.getenv("LLM_ORDER", "gemini,gemini2,claude").split(",")]
    used = [everything[n] for n in order if n in everything]
    return Chain(used, all_providers=list(everything.values()))


_chain: Chain | None = None


def get_chain() -> Chain:
    global _chain
    if _chain is None:
        _chain = build_default_chain()
    return _chain


def set_chain(chain: Chain | None) -> None:
    """Tests install a chain of FakeProviders (or an empty one = rules only)."""
    global _chain
    _chain = chain


def triage_report(cleaned: str, stripped: str) -> LLMResult:
    return get_chain().triage(cleaned, stripped)


def ask_llm_text(prompt: str) -> str | None:
    return get_chain().ask_text(prompt)


def provider_status() -> dict:
    return get_chain().status()
