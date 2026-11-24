r"""Send one sample report through each configured AI provider.

Prints OK / FAIL / QUOTA / MODEL NOT FOUND plus latency. Never prints keys.
Run: .venvScripts\python.exe scriptseck_ai.py
"""
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SAMPLE = "lab 3 ka wifi subah se nai chal rha"
PROMPT = f'Reply with JSON only: {{"problem": "<one short English phrase>"}}. Report: "{SAMPLE}"'


def classify(exc: Exception) -> str:
    text = str(exc).lower()
    code = getattr(exc, "code", None)
    if code == 429 or "resource_exhausted" in text or "quota" in text:
        return "QUOTA"
    if code == 404 or "not found" in text or "not_found" in text:
        return "MODEL NOT FOUND"
    return "FAIL"


def check_gemini(label: str, model: str, key: str, timeout: int) -> None:
    if not key:
        print(f"{label:<9} SKIPPED  (no GEMINI_API_KEY)")
        return
    if not model:
        print(f"{label:<9} SKIPPED  (no model name set)")
        return
    from google import genai
    from google.genai import types

    start = time.perf_counter()
    try:
        client = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=timeout * 1000)
        )
        resp = client.models.generate_content(
            model=model,
            contents=PROMPT,
            config=types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0, max_output_tokens=200
            ),
        )
        ms = int((time.perf_counter() - start) * 1000)
        ok = bool((resp.text or "").strip())
        print(f"{label:<9} {'OK' if ok else 'FAIL (empty reply)':<8} {model}  {ms} ms")
    except Exception as exc:  # report, never crash
        ms = int((time.perf_counter() - start) * 1000)
        print(f"{label:<9} {classify(exc):<8} {model}  {ms} ms  ({type(exc).__name__}, code={getattr(exc, "code", "?")}, status={getattr(exc, "status", "?")})")


def check_claude(model: str, key: str, timeout: int) -> None:
    if not key:
        print(f"{'claude':<9} SKIPPED  (no ANTHROPIC_API_KEY, this is normal)")
        return
    import anthropic

    start = time.perf_counter()
    try:
        client = anthropic.Anthropic(api_key=key, timeout=timeout)
        resp = client.messages.create(
            model=model, max_tokens=100, temperature=0,
            messages=[{"role": "user", "content": PROMPT}],
        )
        ms = int((time.perf_counter() - start) * 1000)
        print(f"{'claude':<9} {'OK' if resp.content else 'FAIL':<8} {model}  {ms} ms")
    except Exception as exc:
        ms = int((time.perf_counter() - start) * 1000)
        print(f"{'claude':<9} {classify(exc):<8} {model}  {ms} ms  ({type(exc).__name__}, code={getattr(exc, "code", "?")}, status={getattr(exc, "status", "?")})")


def main() -> None:
    timeout = int(os.getenv("LLM_TIMEOUT_SECONDS", "12") or 12)
    names = ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]
    gkeys = [(n, os.getenv(n, "")) for n in names]
    gkey = next((k for _, k in gkeys if k), "")
    print(f"Sample report: {SAMPLE!r}\n")
    for n, k in gkeys:
        print(f"[{n}]")
        check_gemini("gemini", os.getenv("GEMINI_MODEL", ""), k, timeout)
        check_gemini("gemini2", os.getenv("GEMINI_MODEL_2", ""), k, timeout)
    check_claude(os.getenv("CLAUDE_MODEL", ""), os.getenv("ANTHROPIC_API_KEY", ""), timeout)
    if not gkey:
        print("\nNo Gemini key found. Put GEMINI_API_KEY in .env, then run this again.")
        print("The app still works without keys (rules mode).")


if __name__ == "__main__":
    main()
