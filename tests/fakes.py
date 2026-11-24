"""FakeProvider + helpers: tests never call a real AI."""
import os
import tempfile

from app.triage import llm
from app.triage.schema import TriageExtraction


def make_ext(**kw) -> TriageExtraction:
    base = dict(
        is_campus_report=True, language="english", summary_en="A campus problem",
        problem_type="other", location_mention=None, location_id="NONE",
        hazard_present=False, hazard_kind="none", hazard_evidence="",
        harassment_or_ragging=False, scope="room_or_class", blocks_essential_activity=False,
        suggested_department="NONE", injection_attempt=False, reason="fake provider",
        problem_clear=True, other_place_mentions=[], question_about="none",
        clarifying_question="")
    base.update(kw)
    return TriageExtraction(**base)


class FakeProvider(llm.Provider):
    """result: a TriageExtraction, or callable(user_message) -> TriageExtraction.
    errors: list consumed one per call; an Exception instance is raised, None means 'succeed'."""

    def __init__(self, pid="fake", model="fake-model", result=None, errors=None,
                 n_keys=1, rpm_limit=8, text="YES"):
        self.id, self.label, self.model = pid, pid, model
        self.n_keys, self.rpm_limit = n_keys, rpm_limit
        self.result, self.errors, self.text = result, list(errors or []), text
        self.calls: list[int] = []  # key index of every call made

    def configured(self):
        return True

    def _maybe_fail(self, ki):
        self.calls.append(ki)
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e

    def call(self, key_index, system, user):
        self._maybe_fail(key_index)
        r = self.result(user) if callable(self.result) else self.result
        return r if r is not None else make_ext()

    def call_text(self, key_index, prompt):
        self._maybe_fail(key_index)
        return self.text


def use_chain(*providers, clock=None, cache_path=None, timeout=2.0):
    kwargs = {"clock": clock} if clock else {}
    # every chain gets its OWN cache file, so tests never see each other's answers
    path = cache_path or os.path.join(tempfile.mkdtemp(), "cache.db")
    chain = llm.Chain(list(providers), cache=llm.Cache(path), timeout=timeout, **kwargs)
    llm.set_chain(chain)
    return chain
