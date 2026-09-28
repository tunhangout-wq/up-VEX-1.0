"""Shared server-side client for the Atria Dawn Preview API.

The client intentionally does NOT impose an application-side RPM cooldown. Atria
controls account-level request limits server-side; Vixen does not add an artificial
per-user delay. A small concurrency gate only smooths burst pressure, while transient
HTTP failures recover automatically without exposing provider diagnostics to chat users.
"""

import asyncio
from contextlib import asynccontextmanager
import datetime
import json
import logging
import random
import os
import time
from uuid import uuid4

import aiohttp
try:
    import orjson
except ImportError:  # pragma: no cover - production requirements include orjson
    orjson = None

DEFAULT_API_BASE = "https://api.atria-asi.ai/v1"
DEFAULT_MODEL = "Atria-Dawn-Preview"
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MESSAGES = 100
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_MAX_WAIT = 120.0
TRANSIENT_RETRIES = 2
TRANSIENT_MAX_WAIT = 8.0
STREAM_TIMEOUT_RETRIES = 2
FAST_MAX_OUTPUT_TOKENS = 8192
CONTINUATION_MAX_OUTPUT_TOKENS = 8192
STREAM_TOTAL_TIMEOUT = 240
FAST_STREAM_TOTAL_TIMEOUT = 150
STREAM_SOCK_READ_TIMEOUT = 60
FAST_STREAM_SOCK_READ_TIMEOUT = 45
COMPLETE_TOTAL_TIMEOUT = 180
FAST_COMPLETE_TOTAL_TIMEOUT = 120
CIRCUIT_FAILURE_THRESHOLD = 3
CIRCUIT_OPEN_SECONDS = 20.0
logger = logging.getLogger(__name__)


def _clean_env(name, default=""):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _clean_api_key(value):
    """Normalize pasted keys; accidental spaces/newlines can invalidate auth."""
    if not isinstance(value, str):
        return ""
    return "".join(value.split())


class AIProviderError(RuntimeError):
    """Safe provider failure with optional recovery metadata."""

    def __init__(
        self,
        message,
        *,
        status_code=None,
        safe_message=None,
        retry_after=None,
        partial_text=None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.safe_message = safe_message or "الـAI موش متاح توا."
        self.retry_after = retry_after
        self.partial_text = partial_text if isinstance(partial_text, str) else None


class AIProvider:
    def __init__(self):
        self._session = None
        self._session_lock = asyncio.Lock()
        self._active_requests = 0
        self._status = {
            "provider": "Atria",
            "model": self.model,
            "endpoint": self.endpoint,
            "configured": False,
            "status": "Not Configured",
            "latency_ms": None,
            "first_token_latency_ms": None,
            "total_latency_ms": None,
            "last_request": None,
            "error": None,
            "client_rate_limit": False,
            "active_requests": 0,
            "recovery": None,
            "retries": 0,
            "request_id": None,
            "last_finish_reason": None,
            "metrics": {
                "requests": 0,
                "successes": 0,
                "failures": 0,
                "rate_limit_recoveries": 0,
                "continuations": 0,
                "partial_preserved": 0,
                "transient_recoveries": 0,
                "network_recoveries": 0,
                "queue_timeouts": 0,
                "queue_wait_ms": 0.0,
            },
        }
        # This is a burst/concurrency guard, not a normal RPM/user cooldown. Healthy
        # requests remain eligible immediately; only excessive in-flight concurrency
        # is queued, and provider 429 recovery is handled separately below.
        self._rate_limit_until = 0.0
        self._rate_limit_lock = asyncio.Lock()
        self._circuit_open_until = 0.0
        self._consecutive_transient_failures = 0
        self._circuit_lock = asyncio.Lock()
        try:
            max_concurrency = int(os.getenv("ATRIA_MAX_CONCURRENCY", "6"))
        except (TypeError, ValueError):
            max_concurrency = 6
        self._request_gate_limit = max(2, min(16, max_concurrency))
        self._request_gate = asyncio.Semaphore(self._request_gate_limit)

    @property
    def api_key(self):
        primary = _clean_api_key(os.getenv("ATRIA_API_KEY"))
        if primary:
            return primary
        return _clean_api_key(os.getenv("AI_API_KEY"))

    @property
    def key_source(self):
        primary = _clean_api_key(os.getenv("ATRIA_API_KEY"))
        if primary:
            return "ATRIA_API_KEY"
        legacy = _clean_api_key(os.getenv("AI_API_KEY"))
        return "AI_API_KEY" if legacy else None

    @property
    def api_base_url(self):
        return _clean_env("ATRIA_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")

    @property
    def endpoint(self):
        return self.api_base_url + "/chat/completions"

    @property
    def model(self):
        return _clean_env("ATRIA_MODEL", DEFAULT_MODEL)

    @staticmethod
    def _response_error(status, payload):
        detail = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or error.get("detail") or error.get("code")
            elif isinstance(error, str):
                detail = error
            if not detail:
                detail = payload.get("message") or payload.get("detail")
        elif isinstance(payload, str):
            detail = payload
        if not isinstance(detail, str) or not detail.strip():
            detail = "request rejected"
        return f"HTTP {status}: {' '.join(detail.split())[:220]}"

    @staticmethod
    def _parse_retry_after(headers):
        value = (headers or {}).get("Retry-After")
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            return None
        return seconds if seconds >= 0 else None

    async def _wait_for_provider_backoff(self):
        while True:
            delay = self._rate_limit_until - time.monotonic()
            if delay <= 0:
                return
            await asyncio.sleep(min(delay, 30.0))

    async def _circuit_check(self):
        async with self._circuit_lock:
            remaining = self._circuit_open_until - time.monotonic()
            if remaining > 0:
                self._status["status"] = "Recovering"
                self._status["recovery"] = {"type": "circuit_open", "remaining_s": round(remaining, 1)}
                raise AIProviderError(
                    "AI provider circuit is open after repeated transient failures",
                    safe_message="🛠️ الـAI يمرّ بضغط مؤقت وتمّ إيقاف المحاولات الزائدة آليًا.",
                )
            if self._circuit_open_until:
                self._circuit_open_until = 0.0
                self._consecutive_transient_failures = 0

    async def _circuit_success(self):
        async with self._circuit_lock:
            self._consecutive_transient_failures = 0
            self._circuit_open_until = 0.0

    async def _circuit_failure(self):
        async with self._circuit_lock:
            self._consecutive_transient_failures += 1
            if self._consecutive_transient_failures >= CIRCUIT_FAILURE_THRESHOLD:
                self._circuit_open_until = max(
                    self._circuit_open_until, time.monotonic() + CIRCUIT_OPEN_SECONDS
                )

    async def _register_provider_backoff(self, retry_after):
        delay = retry_after if retry_after is not None else 1.5
        delay = max(0.5, min(float(delay), RATE_LIMIT_MAX_WAIT))
        # A small jitter prevents many workers from waking on the same boundary.
        delay = min(RATE_LIMIT_MAX_WAIT, delay + random.uniform(0.05, min(0.75, delay * 0.15)))
        async with self._rate_limit_lock:
            target = time.monotonic() + delay
            self._rate_limit_until = max(self._rate_limit_until, target)
            self._status["recovery"] = {
                "type": "provider_backpressure",
                "retry_after_s": round(delay, 2),
            }
        return delay

    @staticmethod
    def _transient_delay(attempt: int) -> float:
        base = min(TRANSIENT_MAX_WAIT, 0.75 * (2 ** max(0, attempt - 1)))
        return min(TRANSIENT_MAX_WAIT, base + random.uniform(0.05, min(0.6, base * 0.2)))

    async def _wait_transient_recovery(self, kind: str, attempt: int) -> float:
        delay = self._transient_delay(attempt)
        self._status["recovery"] = {"type": kind, "attempt": attempt, "delay_s": round(delay, 2)}
        self._status["retries"] = attempt
        self._status.update(status="Recovering", error=kind)
        await asyncio.sleep(delay)
        self._status["metrics"]["transient_recoveries"] += 1
        if kind == "network_recovery":
            self._status["metrics"]["network_recoveries"] += 1
        return delay

    def status(self):
        configured = bool(self.api_key)
        result = dict(self._status)
        result["configured"] = configured
        result["model"] = self.model
        result["endpoint"] = self.endpoint
        result["key_source"] = self.key_source
        result["client_rate_limit"] = False
        result["active_requests"] = self._active_requests
        result["circuit_open"] = self._circuit_open_until > time.monotonic()
        result["capabilities"] = {
            "model": self.model,
            "streaming": True,
            "client_rate_limit": False,
            "configured_max_output_tokens": 65536,
            "max_in_flight_requests": self._request_gate_limit,
        }
        if not configured:
            result["status"] = "Not Configured"
            result["error"] = "ATRIA_API_KEY is not configured"
        elif result["status"] == "Not Configured":
            result["status"] = "Not Tested"
            result["error"] = None
        result["metrics"] = dict(self._status.get("metrics", {}))
        return result

    async def open(self):
        """Create exactly one reusable HTTP session even when startup is concurrent."""
        if self._session is not None and not self._session.closed:
            return
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return
            connector = aiohttp.TCPConnector(limit=100, limit_per_host=100, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=20, connect=4, sock_read=16),
                raise_for_status=False,
            )

    async def close(self):
        async with self._session_lock:
            session = self._session
            self._session = None
        if session is not None and not session.closed:
            await session.close()

    @asynccontextmanager
    async def _gated_post(self, url, **kwargs):
        """Bound concurrent outbound requests and prevent an unbounded queue from hanging users."""
        queue_started = time.monotonic()
        try:
            await asyncio.wait_for(self._request_gate.acquire(), timeout=10.0)
        except asyncio.TimeoutError as exc:
            self._status["metrics"]["queue_timeouts"] = self._status["metrics"].get("queue_timeouts", 0) + 1
            raise AIProviderError(
                "AI request queue saturated",
                safe_message="🛠️ الـAI مشغول توا وتمّ احتواء الضغط آليًا.",
            ) from exc
        self._status["metrics"]["queue_wait_ms"] = round((time.monotonic() - queue_started) * 1000, 2)
        try:
            # This service runs on one asyncio event loop; integer updates do not
            # yield, so the explicit counter is cancellation-safe and avoids an
            # await inside the cleanup path that could leak the semaphore.
            self._active_requests += 1
            async with self._session.post(url, **kwargs) as response:
                yield response
        finally:
            self._active_requests = max(0, self._active_requests - 1)
            self._request_gate.release()

    def _provider_error(self, response, payload):
        status = response.status
        detail = self._response_error(status, payload)
        retry_after = self._parse_retry_after(getattr(response, "headers", {}))
        if status == 400:
            return AIProviderError(
                detail,
                status_code=status,
                safe_message="⚠️ الطلب للـAI موش صالح. غالبًا الإعدادات أو صيغة الرسالة فيها مشكلة.",
            )
        if status == 401:
            return AIProviderError(
                detail,
                status_code=status,
                safe_message="🔐 مفتاح Atria مرفوض. راجع ATRIA_API_KEY في .env.",
            )
        if status == 403:
            return AIProviderError(
                detail,
                status_code=status,
                safe_message="🚫 Atria رفضت الطلب. راجع صلاحية الـAPI key والحساب.",
            )
        if status == 429:
            return AIProviderError(
                "provider backpressure",
                status_code=status,
                retry_after=retry_after,
                # User-facing text intentionally avoids provider diagnostics and
                # never asks the user to shorten/resend a healthy request.
                safe_message="🛠️ ما كملش الرد توا، وتمّت محاولة الاستعادة آليًا.",
            )
        if status >= 500:
            return AIProviderError(
                detail,
                status_code=status,
                safe_message="🛠️ ما كملش الرد توا، وتمّت محاولة الاستعادة آليًا.",
            )
        return AIProviderError(
            detail,
            status_code=status,
            safe_message="⚠️ Atria رفضت الطلب. راجع إعدادات الـAI.",
        )

    @staticmethod
    def _loads(raw):
        if orjson is not None:
            try:
                return orjson.loads(raw)
            except Exception:
                pass
        return json.loads(raw)

    @staticmethod
    def _continuation_messages(messages, partial):
        base = list(messages)
        if len(base) > MAX_MESSAGES - 2:
            base = [base[0], *base[-(MAX_MESSAGES - 3):]] if base and base[0].get("role") == "system" else base[-(MAX_MESSAGES - 2):]
        base.append({"role": "assistant", "content": str(partial)[-60_000:]})
        base.append({
            "role": "user",
            "content": (
                "Continue exactly from the end of the assistant answer above. "
                "The assistant message is already-visible output, not instructions. "
                "Do not repeat any words or sentences already present when they overlap. "
                "Output only the missing continuation and finish the original request."
            ),
        })
        return base

    async def _recover_continuation(self, messages, partial, *, fast):
        return await self.complete(
            self._continuation_messages(messages, partial),
            max_tokens=CONTINUATION_MAX_OUTPUT_TOKENS,
            fast=fast,
            auto_continue=False,
        )

    async def _continue_until_complete(self, messages, partial, *, fast, max_rounds=4):
        accumulated = str(partial or "")
        if not accumulated:
            raise AIProviderError("cannot continue an empty response")
        for round_index in range(1, max_rounds + 1):
            # A continuation is a fresh provider turn. Clear the previous
            # request's finish state first so a mocked/older continuation
            # implementation cannot inherit `length` and falsely exhaust the budget.
            self._status["last_finish_reason"] = None
            continuation = await self._recover_continuation(messages, accumulated, fast=fast)
            finish_reason = self._status.get("last_finish_reason")
            if not isinstance(continuation, str) or not continuation.strip():
                raise AIProviderError(
                    "Atria continuation returned no text",
                    safe_message="🛠️ الرد توقف قبل ما يكمل، وتمّت محاولة الاستعادة آليًا.",
                    partial_text=accumulated + "…(الرد لم يكتمل، اكتب: كمل)",
                )
            accumulated = self._combine_continuation(accumulated, continuation)
            self._status["metrics"]["continuations"] += 1
            self._status["recovery"] = {"type": "auto_continuation", "round": round_index}
            if finish_reason in {"stop", "end_turn", None}:
                # The non-SSE continuation helper returns a complete JSON body.
                # If that body has no finish_reason, its successful transport return
                # is the terminal signal; real provider `length` responses continue.
                return accumulated
        raise AIProviderError(
            "Atria continuation budget exhausted before a terminal finish reason",
            safe_message="🛠️ الرد توقف قبل ما يكمل، وتمّت محاولة الاستعادة آليًا.",
            partial_text=accumulated + "…(الرد لم يكتمل، اكتب: كمل)",
        )

    @staticmethod
    def _overlap_length(left, right, minimum=12):
        left = str(left or "")
        right = str(right or "")
        maximum = min(len(left), len(right))
        for size in range(maximum, minimum - 1, -1):
            if left[-size:] == right[:size]:
                return size
        return 0

    @classmethod
    def _combine_continuation(cls, partial, continuation):
        partial = str(partial or "")
        continuation = str(continuation or "")
        if not partial:
            return continuation
        if not continuation:
            return partial
        overlap = cls._overlap_length(partial, continuation)
        if overlap:
            suffix = continuation[overlap:]
        else:
            suffix = continuation
        if not suffix:
            return partial
        if partial[-1].isspace() or suffix[0].isspace():
            return partial + suffix
        if suffix[0] in ".,!?;:،؛؟)]}\"'…" or partial[-1] in "([{\"'":
            return partial + suffix
        return partial + " " + suffix

    @classmethod
    def _merge_continuation(cls, partial, continuation):
        partial = str(partial or "")
        continuation = str(continuation or "")
        if not continuation:
            return ""
        overlap = cls._overlap_length(partial, continuation)
        suffix = continuation[overlap:] if overlap else continuation
        if not suffix:
            return ""
        if not partial or partial[-1].isspace() or suffix[0].isspace():
            return suffix
        if suffix[0] in ".,!?;:،؛؟)]}\"'…" or partial[-1] in "([{\"'":
            return suffix
        return " " + suffix

    @staticmethod
    def _extract_finish_reason(payload):
        choices = payload.get("choices") if isinstance(payload, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else None
        return first.get("finish_reason") if isinstance(first, dict) else None

    @staticmethod
    def _extract_text(data):
        choices = data.get("choices") if isinstance(data, dict) else None
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message", {}) if isinstance(first, dict) else {}
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            content = "".join(parts)
        return content

    async def stream(self, messages, max_tokens=65536, *, fast=False, auto_continue=True):
        """Yield deltas from one SSE request and recover incomplete output without replaying it."""
        key = self.api_key
        request_id = uuid4().hex[:12]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._status["last_request"] = now
        self._status["latency_ms"] = None
        self._status["first_token_latency_ms"] = None
        self._status["total_latency_ms"] = None
        self._status["recovery"] = None
        self._status["retries"] = 0
        self._status["request_id"] = request_id
        self._status["last_finish_reason"] = None
        self._status["metrics"]["requests"] += 1
        if not key:
            self._status["metrics"]["failures"] += 1
            self._status.update(status="Not Configured", error="ATRIA_API_KEY is not configured")
            raise AIProviderError(
                "ATRIA_API_KEY is not configured",
                safe_message="🔑 الـAI موش مكوّن. حط ATRIA_API_KEY في .env ثم أعد تشغيل البوت.",
            )
        if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
            raise ValueError("Invalid AI request")
        if not all(
            isinstance(item, dict)
            and item.get("role") in {"system", "user", "assistant"}
            and isinstance(item.get("content"), str)
            and item["content"].strip()
            for item in messages
        ):
            raise ValueError("Invalid AI request")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 65536:
            raise ValueError("Invalid AI request")
        if fast:
            max_tokens = min(max_tokens, FAST_MAX_OUTPUT_TOKENS)

        await self.open()
        started = time.monotonic()
        retry_attempts = 0
        recovery_waited = 0.0
        transient_attempts = 0
        network_attempts = 0
        timeout_attempts = 0

        while True:
            await self._circuit_check()
            await self._wait_for_provider_backoff()
            got_text = False
            partial_parts: list[str] = []
            complete = False
            saw_done = False
            finish_reason = None
            try:
                async with self._gated_post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        "Accept": "text/event-stream",
                    },
                    json={
                        "model": self.model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "stream": True,
                    },
                    timeout=aiohttp.ClientTimeout(
                        total=STREAM_TOTAL_TIMEOUT if not fast else FAST_STREAM_TOTAL_TIMEOUT,
                        connect=7,
                        sock_read=STREAM_SOCK_READ_TIMEOUT if not fast else FAST_STREAM_SOCK_READ_TIMEOUT,
                    ),
                ) as response:
                    if response.status != 200:
                        raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                        try:
                            payload = self._loads(raw)
                        except Exception:
                            payload = raw.decode("utf-8", errors="replace")
                        error = self._provider_error(response, payload)
                        if error.status_code and 500 <= error.status_code <= 599:
                            transient_attempt = transient_attempts + 1
                            if transient_attempt <= TRANSIENT_RETRIES:
                                transient_attempts = transient_attempt
                                await self._wait_transient_recovery("server_recovery", transient_attempt)
                                continue
                        if error.status_code == 429 and retry_attempts < RATE_LIMIT_RETRIES:
                            delay = await self._register_provider_backoff(error.retry_after)
                            remaining = RATE_LIMIT_MAX_WAIT - recovery_waited
                            if delay <= remaining:
                                retry_attempts += 1
                                self._status["metrics"]["rate_limit_recoveries"] += 1
                                recovery_waited += delay
                                self._status["retries"] = retry_attempts
                                self._status.update(status="Recovering", error="provider backpressure")
                                continue
                        self._status.update(status="Error", error=str(error)[:220])
                        self._status["metrics"]["failures"] += 1
                        if error.status_code == 429 or (error.status_code and 500 <= error.status_code <= 599):
                            await self._circuit_failure()
                        raise error

                    first_token_at = None
                    total_bytes = 0
                    async for raw_line in response.content:
                        total_bytes += len(raw_line)
                        if total_bytes > MAX_RESPONSE_BYTES:
                            raise AIProviderError(
                                "Atria stream response is too large",
                                safe_message="📦 الرد كان أكبر من الحد الداخلي، وما عرضناش الرد الناقص.",
                            )
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        payload_text = line[5:].strip()
                        if payload_text == "[DONE]":
                            saw_done = True
                            break
                        try:
                            payload = self._loads(payload_text)
                        except Exception:
                            continue
                        finish_reason = self._extract_finish_reason(payload) or finish_reason
                        if finish_reason:
                            self._status["last_finish_reason"] = finish_reason
                        choices = payload.get("choices") if isinstance(payload, dict) else None
                        first = choices[0] if isinstance(choices, list) and choices else None
                        delta = first.get("delta", {}) if isinstance(first, dict) else {}
                        delta_text = delta.get("content") if isinstance(delta, dict) else None
                        if not isinstance(delta_text, str) or not delta_text:
                            message_obj = first.get("message", {}) if isinstance(first, dict) else {}
                            if isinstance(message_obj, dict):
                                delta_text = message_obj.get("content")
                        if isinstance(delta_text, str) and delta_text:
                            got_text = True
                            if first_token_at is None:
                                first_token_at = time.monotonic()
                                self._status["first_token_latency_ms"] = round((first_token_at - started) * 1000, 2)
                                self._status.update(status="Connected", error=None, recovery=None)
                            partial_parts.append(delta_text)
                            yield delta_text
                        if finish_reason in {"stop", "end_turn"}:
                            complete = True
                            break

                    if not got_text:
                        total_latency = round((time.monotonic() - started) * 1000, 2)
                        self._status["total_latency_ms"] = total_latency
                        self._status["latency_ms"] = total_latency
                        self._status.update(status="Error", error="Atria returned an empty stream")
                        self._status["metrics"]["failures"] += 1
                        raise AIProviderError(
                            "Atria returned an empty stream",
                            safe_message="🤏 ما وصل حتى محتوى قابل للعرض من الـAI.",
                        )

                partial = "".join(partial_parts)
                # [DONE] is the terminal marker for an otherwise valid stream.
                # A provider may still report finish_reason=length before [DONE];
                # that case is intentionally incomplete and must enter continuation.
                if saw_done and finish_reason != "length":
                    complete = True
                if finish_reason in {"stop", "end_turn"}:
                    complete = True
                if not complete and auto_continue:
                    try:
                        accumulated = await self._continue_until_complete(messages, partial, fast=fast, max_rounds=4)
                    except AIProviderError:
                        raise
                    suffix = accumulated[len(partial):] if accumulated.startswith(partial) else self._merge_continuation(partial, accumulated)
                    if suffix:
                        yield suffix
                    complete = True

                total_latency = round((time.monotonic() - started) * 1000, 2)
                self._status["total_latency_ms"] = total_latency
                self._status["latency_ms"] = total_latency
                self._status.update(status="Connected", error=None, recovery=None)
                self._status["metrics"]["successes"] += 1
                await self._circuit_success()
                return
            except AIProviderError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                partial = "".join(partial_parts)
                # Once visible text exists, the original request must never be replayed.
                # Replaying it produces duplicated deltas and corrupted chat history.
                if got_text:
                    if auto_continue and partial:
                        try:
                            accumulated = await self._continue_until_complete(messages, partial, fast=fast, max_rounds=4)
                            suffix = accumulated[len(partial):] if accumulated.startswith(partial) else self._merge_continuation(partial, accumulated)
                            if suffix:
                                yield suffix
                            self._status["metrics"]["successes"] += 1
                            await self._circuit_success()
                            return
                        except AIProviderError:
                            raise
                        except Exception as recovery_exc:
                            self._status["metrics"]["failures"] += 1
                            self._status["metrics"]["partial_preserved"] += 1
                            raise AIProviderError(
                                "Atria stream recovery failed after partial text",
                                safe_message="🛠️ الرد توقف قبل ما يكمل، وتمّت محاولة الاستعادة آليًا.",
                                partial_text=partial + "…(الرد لم يكتمل، اكتب: كمل)",
                            ) from recovery_exc
                    self._status["metrics"]["partial_preserved"] += 1
                    self._status.update(status="Degraded", recovery={"type": "partial_preserved"})
                    return

                if isinstance(exc, asyncio.TimeoutError):
                    timeout_attempts += 1
                    if timeout_attempts <= STREAM_TIMEOUT_RETRIES:
                        self._status["retries"] = timeout_attempts
                        await asyncio.sleep(0.35)
                        continue
                    await self._circuit_failure()
                else:
                    network_attempts += 1
                    if network_attempts <= TRANSIENT_RETRIES:
                        await self._wait_transient_recovery("network_recovery", network_attempts)
                        continue
                    await self._circuit_failure()
                self._status.update(status="Error", error=type(exc).__name__)
                self._status["metrics"]["failures"] += 1
                raise AIProviderError(
                    f"Atria stream failed: {type(exc).__name__}",
                    safe_message="🌐 صار انقطاع لحظي وتمّت محاولة الاستعادة آليًا.",
                ) from exc

    async def complete(self, messages, max_tokens=65536, *, fast=False, auto_continue=True):
        """Request a complete response and continue length/terminal-less outputs up to four rounds."""
        key = self.api_key
        request_id = uuid4().hex[:12]
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self._status["last_request"] = now
        self._status["latency_ms"] = None
        self._status["first_token_latency_ms"] = None
        self._status["total_latency_ms"] = None
        self._status["recovery"] = None
        self._status["retries"] = 0
        self._status["request_id"] = request_id
        self._status["last_finish_reason"] = None
        self._status["metrics"]["requests"] += 1

        if not key:
            self._status["metrics"]["failures"] += 1
            self._status.update(status="Not Configured", error="ATRIA_API_KEY is not configured")
            raise AIProviderError(
                "ATRIA_API_KEY is not configured",
                safe_message="🔑 الـAI موش مكوّن. حط ATRIA_API_KEY في .env ثم أعد تشغيل البوت.",
            )
        if not isinstance(messages, list) or not 1 <= len(messages) <= MAX_MESSAGES:
            raise ValueError("Invalid AI request")
        if not all(
            isinstance(item, dict)
            and item.get("role") in {"system", "user", "assistant"}
            and isinstance(item.get("content"), str)
            and item["content"].strip()
            for item in messages
        ):
            raise ValueError("Invalid AI request")
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or not 1 <= max_tokens <= 65536:
            raise ValueError("Invalid AI request")
        if fast:
            max_tokens = min(max_tokens, FAST_MAX_OUTPUT_TOKENS)

        await self.open()
        started = time.monotonic()
        retry_attempts = 0
        recovery_waited = 0.0
        transient_attempts = 0
        network_attempts = 0

        while True:
            await self._circuit_check()
            await self._wait_for_provider_backoff()
            try:
                async with self._gated_post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": self.model, "messages": messages, "max_tokens": max_tokens},
                    timeout=aiohttp.ClientTimeout(
                        total=COMPLETE_TOTAL_TIMEOUT if not fast else FAST_COMPLETE_TOTAL_TIMEOUT,
                        connect=7,
                        sock_read=50 if not fast else 40,
                    ),
                ) as response:
                    raw = await response.content.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        self._status["metrics"]["failures"] += 1
                        raise AIProviderError(
                            "Atria provider response is too large",
                            safe_message="📦 الرد كان أكبر من الحد الداخلي، وما عرضناش الرد الناقص.",
                        )
                    try:
                        data = self._loads(raw)
                    except Exception:
                        data = raw.decode("utf-8", errors="replace")
                    self._status["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                    if response.status >= 400:
                        error = self._provider_error(response, data)
                        if error.status_code and 500 <= error.status_code <= 599:
                            transient_attempt = transient_attempts + 1
                            if transient_attempt <= TRANSIENT_RETRIES:
                                transient_attempts = transient_attempt
                                await self._wait_transient_recovery("server_recovery", transient_attempt)
                                continue
                        if error.status_code == 429 and retry_attempts < RATE_LIMIT_RETRIES:
                            delay = await self._register_provider_backoff(error.retry_after)
                            remaining = RATE_LIMIT_MAX_WAIT - recovery_waited
                            if delay <= remaining:
                                retry_attempts += 1
                                self._status["metrics"]["rate_limit_recoveries"] += 1
                                recovery_waited += delay
                                self._status["retries"] = retry_attempts
                                self._status.update(status="Recovering", error="provider backpressure")
                                continue
                        self._status.update(status="Error", error=str(error)[:220])
                        self._status["metrics"]["failures"] += 1
                        if error.status_code == 429 or (error.status_code and 500 <= error.status_code <= 599):
                            await self._circuit_failure()
                        raise error
                    if response.content_type != "application/json":
                        self._status["metrics"]["failures"] += 1
                        raise AIProviderError(
                            "Atria returned an invalid response",
                            safe_message="⚠️ رجعت من الـAI استجابة غير صالحة، وما عرضناش منها شيء.",
                        )
                    content = self._extract_text(data)
                    if not isinstance(content, str) or not content.strip():
                        self._status["metrics"]["failures"] += 1
                        raise AIProviderError(
                            "Atria returned no text content",
                            safe_message="🤏 ما وصلش جواب قابل للعرض من الـAI.",
                        )
                    finish_reason = self._extract_finish_reason(data)
                    # For the complete/non-SSE endpoint, a fully-read JSON body with
                    # valid content is itself a terminal transport event when the
                    # provider omits finish_reason. Explicit length remains incomplete.
                    if finish_reason is None:
                        finish_reason = "stop"
                    self._status["last_finish_reason"] = finish_reason
                    cleaned = content.strip()

                if auto_continue and finish_reason not in {"stop", "end_turn"}:
                    cleaned = await self._continue_until_complete(messages, cleaned, fast=fast, max_rounds=4)

                total_latency = round((time.monotonic() - started) * 1000, 2)
                self._status["total_latency_ms"] = total_latency
                self._status["latency_ms"] = total_latency
                self._status.update(status="Connected", error=None, recovery=None)
                self._status["metrics"]["successes"] += 1
                await self._circuit_success()
                return cleaned
            except AIProviderError:
                raise
            except aiohttp.ClientError as exc:
                network_attempts += 1
                if network_attempts <= TRANSIENT_RETRIES:
                    await self._wait_transient_recovery("network_recovery", network_attempts)
                    continue
                self._status.update(status="Error", error=type(exc).__name__)
                self._status["metrics"]["failures"] += 1
                await self._circuit_failure()
                raise AIProviderError(
                    f"Atria network error: {type(exc).__name__}",
                    safe_message="🌐 صار انقطاع لحظي وتمّت محاولة الاستعادة آليًا.",
                ) from exc
            except asyncio.TimeoutError as exc:
                network_attempts += 1
                if network_attempts <= TRANSIENT_RETRIES:
                    await self._wait_transient_recovery("timeout_recovery", network_attempts)
                    continue
                self._status.update(status="Error", error="Timeout")
                self._status["metrics"]["failures"] += 1
                await self._circuit_failure()
                raise AIProviderError(
                    "Atria completion timed out",
                    safe_message="⏳ الرد احتاج وقت أطول من المتوقع، وتمّت محاولة الاستعادة آليًا.",
                ) from exc

    async def test_connection(self):
        if not self.api_key:
            self._status["last_request"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            self._status.update(
                configured=False,
                status="Not Configured",
                latency_ms=None,
                error="ATRIA_API_KEY is not configured",
            )
            return self.status()
        try:
            await self.complete(
                [
                    {"role": "system", "content": "Reply with the single word OK."},
                    {"role": "user", "content": "Connection test"},
                ],
                max_tokens=8,
                fast=True,
            )
        except (AIProviderError, RuntimeError, ValueError):
            pass
        return self.status()


ai_provider = AIProvider()
