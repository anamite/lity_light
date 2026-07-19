"""OpenAI-compatible chat client. OpenRouter today, Ollama/llama.cpp tomorrow —
same wire format, so swapping providers is a config change."""

import asyncio
import os

import httpx


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg):
        self.base_url = cfg.get_path("provider.base_url", "https://openrouter.ai/api/v1").rstrip("/")
        key_env = cfg.get_path("provider.api_key_env", "OPENROUTER_API_KEY")
        self.api_key = os.environ.get(key_env, "")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0))
        self.on_usage = None  # async (model, purpose, usage) -> None; set by App

    async def close(self):
        await self.client.aclose()

    async def chat(self, model: str, messages: list[dict], tools: list[dict] | None = None,
                   max_tokens: int = 4096, temperature: float = 0.6,
                   purpose: str = "") -> tuple[dict, dict]:
        """Returns (assistant_message, usage). Retries transient failures once."""
        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": "https://github.com/lity",
            "X-Title": "Lity",
        }
        last_err = None
        for attempt in range(3):
            try:
                r = await self.client.post(f"{self.base_url}/chat/completions",
                                           json=payload, headers=headers)
                if r.status_code in (429, 500, 502, 503):
                    last_err = LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                r.raise_for_status()
                data = r.json()
                if "choices" not in data or not data["choices"]:
                    raise LLMError(f"malformed response: {str(data)[:300]}")
                usage = data.get("usage", {}) or {}
                if self.on_usage and usage:
                    try:
                        await self.on_usage(model, purpose, usage)
                    except Exception:
                        pass  # accounting must never break a turn
                return data["choices"][0]["message"], usage
            except (httpx.TransportError, httpx.ReadTimeout) as e:
                last_err = e
                await asyncio.sleep(2 * (attempt + 1))
        raise LLMError(f"LLM call failed after retries: {last_err}")

    async def complete(self, model: str, system: str, user: str, max_tokens: int = 1024,
                       purpose: str = "utility") -> str:
        """One-shot text completion for utility jobs (summaries, extraction, heartbeat)."""
        msg, _ = await self.chat(
            model,
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.2,
            purpose=purpose,
        )
        return (msg.get("content") or "").strip()
