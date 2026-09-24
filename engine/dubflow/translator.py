from __future__ import annotations

import json
import re
import time
from typing import Callable, List, Optional

import httpx
from urllib.parse import urlencode

from .asr.base import Transcript
from .config import settings

BATCH_SIZE = 20
ProgressFn = Optional[Callable[[float, str], None]]


class TranslationError(RuntimeError):
    pass


class LLMTranslator:
    """OpenAI-compatible chat completions translator with context-window batching.

    Works with OpenAI, DeepSeek, Ollama (/v1), or any compatible endpoint.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        target_lang: str,
        source_lang: Optional[str] = None,
        temperature: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.target_lang = target_lang
        self.source_lang = source_lang or "the source language"
        self.temperature = temperature
        self._use_responses = False  # auto-set when only the Responses endpoint is allowed

    _LLM_RETRY_DELAYS = (2.0, 10.0, 30.0)

    def _post_with_retry(self, url: str, payload: dict) -> httpx.Response:
        """POST with backoff retry on network errors and 5xx (transient)."""
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        last: Exception = TranslationError("LLM request failed")
        for attempt in range(len(self._LLM_RETRY_DELAYS) + 1):
            if attempt:
                time.sleep(self._LLM_RETRY_DELAYS[attempt - 1])
            try:
                resp = httpx.post(url, headers=headers, json=payload, timeout=180)
            except httpx.HTTPError as e:
                last = e
                continue
            if resp.status_code >= 500:
                last = TranslationError(f"LLM API {resp.status_code}: {resp.text[:200]}")
                continue
            return resp
        raise last

    def _chat(self, messages: list) -> str:
        if self._use_responses:
            return self._chat_responses(messages)
        resp = self._post_with_retry(
            f"{self.base_url}/chat/completions",
            {"model": self.model, "temperature": self.temperature, "messages": messages},
        )
        if resp.status_code in (403, 404, 405):
            # Coding-plan keys (e.g. zhipu) often only allow the Responses
            # endpoint — retry once with /responses before giving up.
            try:
                out = self._chat_responses(messages)
                self._use_responses = True
                return out
            except TranslationError:
                pass
        if resp.status_code != 200:
            if resp.status_code in (404, 405):
                hint = (f" 请求地址: {url} —— Base URL 可能不对：通常需要以 /v1 结尾，"
                        f"如 https://api.openai.com/v1 / https://api.deepseek.com/v1 / http://127.0.0.1:11434/v1，"
                        "且不要包含 /chat/completions 后缀")
            else:
                hint = ""
            raise TranslationError(f"LLM API {resp.status_code}: {resp.text[:200]}{hint}")
        return resp.json()["choices"][0]["message"]["content"]

    def _chat_responses(self, messages: list) -> str:
        """OpenAI Responses API (used by zhipu coding-plan keys)."""
        resp = self._post_with_retry(
            f"{self.base_url}/responses",
            {"model": self.model, "temperature": self.temperature, "input": messages},
        )
        if resp.status_code != 200:
            raise TranslationError(f"LLM responses API {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        texts: list = []
        for item in data.get("output", []):
            if item.get("type") == "message":
                for part in item.get("content", []):
                    if part.get("type") == "output_text":
                        texts.append(part.get("text", ""))
        out = "".join(texts).strip()
        if not out:
            raise TranslationError("empty response from responses endpoint")
        return out

    LLM_BATCH_SIZE = 40  # ~8s per batch against zhipu responses endpoint

    def translate_transcript(self, transcript: Transcript, progress: ProgressFn = None) -> List[str]:
        """Large JSON batches; failed chunks are split recursively until done."""
        texts = [s.text for s in transcript.segments]
        total = len(texts)
        results: List[Optional[str]] = [None] * total
        ranges = [(0, total)] if total else []
        while ranges:
            lo, hi = ranges.pop(0)
            chunk = texts[lo:hi]
            try:
                trans = self._translate_chunk_json(chunk)
            except Exception:
                if hi - lo > 1:
                    mid = (lo + hi) // 2
                    ranges.insert(0, (mid, hi))
                    ranges.insert(0, (lo, mid))
                    continue
                trans = [self._chat([
                    {"role": "system", "content": "You are a professional subtitle translator. Reply with the translation only."},
                    {"role": "user", "content": f"Translate into {self.target_lang}:\n{t}"},
                ]).strip() for t in chunk]
            results[lo:hi] = trans
            if progress:
                done = sum(1 for r in results if r is not None)
                progress(min(done / max(total, 1), 1.0), f"{done}/{total} lines")
        return [r or "" for r in results]

    def _translate_chunk_json(self, chunk: List[str]) -> List[str]:
        items = [{"i": i + 1, "t": t} for i, t in enumerate(chunk)]
        system = (
            "You are a professional subtitle translator. "
            f"Translate each item's 't' value from {self.source_lang or 'the source language'} into {self.target_lang}. "
            'Return STRICTLY a JSON array of objects with identical "i" values in the '
            'same order: [{"i": 1, "t": "translation"}]. No markdown fences, no commentary. '
            "Keep translations concise and natural for subtitles."
        )
        raw = self._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ]).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
        start, end = raw.find("["), raw.rfind("]")
        if start == -1 or end <= start:
            raise TranslationError("no JSON array in LLM response")
        arr = json.loads(raw[start:end + 1])
        if not isinstance(arr, list) or len(arr) != len(chunk):
            raise TranslationError("json length mismatch")
        out: List[str] = [""] * len(chunk)
        seen = set()
        for item in arr:
            if not isinstance(item, dict):
                raise TranslationError("bad item type")
            i = int(item.get("i", -1))
            t = item.get("t")
            if not (1 <= i <= len(chunk)) or not isinstance(t, str) or i in seen:
                raise TranslationError("bad or duplicate id")
            seen.add(i)
            out[i - 1] = t.strip()
        if len(seen) != len(chunk):
            raise TranslationError("missing ids in response")
        return out

    def _translate_batch(self, batch) -> List[str]:
        numbered = "\n".join(f"{i + 1}. {s.text}" for i, s in enumerate(batch))
        system = (
            "You are a professional subtitle translator. "
            f"Translate each numbered line from {self.source_lang} into {self.target_lang}. "
            "Rules: keep the same numbering, one output line per input line, "
            "concise natural spoken style suitable for subtitles, no extra commentary."
        )
        content = self._chat([
            {"role": "system", "content": system},
            {"role": "user", "content": numbered},
        ])
        parsed = self._parse_numbered(content, len(batch))
        if parsed is not None:
            return parsed
        # count mismatch -> per-line fallback
        out = []
        for s in batch:
            out.append(self._chat([
                {"role": "system", "content": system},
                {"role": "user", "content": f"1. {s.text}"},
            ]).strip())
        return out

    @staticmethod
    def _parse_numbered(text: str, expected: int) -> Optional[List[str]]:
        found: dict = {}
        last = 0
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            m = re.match(r"^(\d+)\s*[.、):：]\s*(.*)$", line)
            if m:
                idx = int(m.group(1))
                found[idx] = m.group(2).strip()
                last = idx
            elif last:
                found[last] = (found[last] + " " + line).strip()
        if len(found) != expected:
            return None
        return [found[i] for i in range(1, expected + 1)]


_UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}


class GoogleTranslator:
    """Free Google endpoint (translate.googleapis.com), no key.
    Note: unreachable from mainland China; use microsoft there."""

    def __init__(self, target_lang: str, source_lang: Optional[str] = None) -> None:
        codes = {"zh": "zh-CN"}
        self.target = codes.get(target_lang, target_lang)
        self.source = codes.get(source_lang or "", source_lang) if source_lang else "auto"
        self._client = httpx.Client(timeout=60, headers=_UA, follow_redirects=True)

    def translate_transcript(self, transcript: Transcript, progress: ProgressFn = None) -> List[str]:
        texts = [s.text for s in transcript.segments]
        out: List[str] = []
        total = len(texts)
        for i in range(0, total, 40):
            batch = texts[i:i + 40]
            try:
                out.extend(self._translate_batch(batch))
            except Exception:
                time.sleep(2.0)  # back off before per-line fallback
                out.extend([self._translate_one(t) for t in batch])
            if progress:
                progress(min(len(out) / max(total, 1), 1.0), f"{len(out)}/{total} lines")
            time.sleep(0.2)  # pacing between LLM batches
        return out

    _RETRY_DELAYS = (2.0, 8.0, 30.0)

    def _post_retry(self, url: str, params: dict, body: str):
        last: Optional[Exception] = None
        for attempt in range(len(self._RETRY_DELAYS) + 1):
            if attempt:
                time.sleep(self._RETRY_DELAYS[attempt - 1])
            resp = self._client.post(url, params=params, content=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
            if resp.status_code == 429 or resp.status_code >= 500:
                last = TranslationError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp
        raise last if last else TranslationError("google translate request failed")

    def _get_retry(self, url: str, params: dict):
        last: Optional[Exception] = None
        for attempt in range(len(self._RETRY_DELAYS) + 1):
            if attempt:
                time.sleep(self._RETRY_DELAYS[attempt - 1])
            resp = self._client.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                last = TranslationError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            return resp
        raise last if last else TranslationError("google translate request failed")

    def _translate_batch(self, batch: List[str]) -> List[str]:
        # NOTE: content= with manual urlencode - httpx mangles data= lists via proxies
        body = urlencode([("q", t) for t in batch])
        resp = self._post_retry(
            "https://translate.googleapis.com/translate_a/t",
            {"client": "dict-chrome-ex", "sl": self.source, "tl": self.target},
            body,
        )
        arr = resp.json()
        if isinstance(arr, list) and len(arr) == len(batch) \
                and all(isinstance(x, str) for x in arr):
            return arr
        raise TranslationError("unexpected google response shape")

    def _translate_one(self, text: str) -> str:
        resp = self._get_retry(
            "https://translate.googleapis.com/translate_a/single",
            {"client": "gtx", "sl": self.source, "tl": self.target, "dt": "t", "q": text},
        )
        return "".join(part[0] for part in resp.json()[0] if part and part[0])


class MicrosoftTranslator:
    """Microsoft Translator via the official Azure Translator API.
    Requires a (free-tier OK) subscription key:
      env DUBFLOW_MSFT_TRANSLATOR_KEY / DUBFLOW_MSFT_TRANSLATOR_REGION
      or per-request translation_options api_key / region.
    Region: e.g. "global" or the resource's region (eastasia ...)."""

    HOST = "https://api.cognitive.microsofttranslator.com"

    def __init__(self, target_lang: str, source_lang: Optional[str] = None,
                 api_key: str = "", region: str = "") -> None:
        codes = {"zh": "zh-Hans"}
        self.target = codes.get(target_lang, target_lang)
        self.source = codes.get(source_lang or "", source_lang) if source_lang else None
        self.api_key = api_key or settings.msft_translator_key
        self.region = region or settings.msft_translator_region
        self._client = httpx.Client(timeout=60, trust_env=True)
        if not self.api_key:
            raise TranslationError(
                "Microsoft translation needs an Azure Translator key "
                "(free F0 tier works). Set DUBFLOW_MSFT_TRANSLATOR_KEY "
                "or fill the key field in the GUI."
            )

    def translate_transcript(self, transcript: Transcript, progress: ProgressFn = None) -> List[str]:
        texts = [s.text for s in transcript.segments]
        out: List[str] = []
        total = len(texts)
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/json",
        }
        if self.region and self.region.lower() != "global":
            headers["Ocp-Apim-Subscription-Region"] = self.region
        for i in range(0, total, 100):   # Azure allows up to 100 texts / request
            batch = texts[i:i + 100]
            params = {"api-version": "3.0", "to": [self.target]}
            if self.source:
                params["from"] = self.source
            resp = self._client.post(f"{self.HOST}/translate", params=params,
                                     headers=headers,
                                     json=[{"Text": t} for t in batch])
            if resp.status_code != 200:
                raise TranslationError(f"Azure translator {resp.status_code}: {resp.text[:300]}")
            for item in resp.json():
                tr = item.get("translations") or [{}]
                out.append(str(tr[0].get("text", "")))
            if progress:
                progress(min(len(out) / max(total, 1), 1.0), f"{len(out)}/{total} lines")
        return out


def build_translator(t_opts: dict, target_lang: str, source_lang: Optional[str] = None):
    """Factory: llm | google | microsoft -> object with translate_transcript()."""
    provider = (t_opts.get("provider") or "llm").lower()
    if provider == "google":
        return GoogleTranslator(target_lang, source_lang)
    if provider == "microsoft":
        return MicrosoftTranslator(target_lang, source_lang,
                                   api_key=t_opts.get("api_key") or "",
                                   region=t_opts.get("region") or "")
    return LLMTranslator(
        base_url=t_opts.get("base_url") or settings.translate_base_url,
        api_key=t_opts.get("api_key") or settings.translate_api_key,
        model=t_opts.get("model") or settings.translate_model,
        target_lang=target_lang,
        source_lang=source_lang,
    )
