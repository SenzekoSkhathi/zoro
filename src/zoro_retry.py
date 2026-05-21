"""Bounded retry for transient network failures on idempotent tool reads.

Applied *only* to read-only / safe-to-repeat tools — never to anything that
mutates state. The contract: if a call raises a transient httpx error OR
the tool's spoken reply looks like a transient apology, retry once after
a short backoff. The wrapped tool sees nothing different; the `_logged`
decorator above it logs the final outcome only.

Phase 2 stays conservative on purpose — once real corrected-episode data
is in, we can extend this to argument-mutating retries (widening searches,
falling back to a different account, etc.) tuned on what actually fails.
"""

from __future__ import annotations

import asyncio
import functools
import re

import httpx

# httpx exceptions that almost always mean "the network blinked, try again".
# Not retried: HTTPStatusError 4xx (the request itself is wrong) — they
# won't get better with another attempt.
_TRANSIENT_EXC = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)

# Result strings that strongly indicate a transient outer-layer failure
# (typically a `My X failed: <httpx error>` reply). Anchored to refusal
# patterns we know wrap exceptions, not deliberate "no results" answers.
_TRANSIENT_RESULT_RE = re.compile(
    r"(?i)(timed? out|temporarily|network|connection (?:reset|refused|aborted)"
    r"|read timeout|httpx\.|503\b|504\b|502\b)"
)


def _is_transient_5xx(exc: BaseException) -> bool:
    return (
        isinstance(exc, httpx.HTTPStatusError)
        and 500 <= exc.response.status_code < 600
    )


def transient(attempts: int = 2, backoff: float = 0.4):
    """Decorator: retry the wrapped tool body on transient failures.

    Apply UNDER @_logged so the episode row reflects the final outcome
    (success after retry shows as success, not a logged failure-then-retry).
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(self, context, *args, **kwargs):
            last_exc: BaseException | None = None
            for attempt in range(max(1, attempts)):
                try:
                    result = await fn(self, context, *args, **kwargs)
                except _TRANSIENT_EXC as e:
                    last_exc = e
                    if attempt + 1 >= attempts:
                        raise
                    await asyncio.sleep(backoff * (attempt + 1))
                    continue
                except httpx.HTTPStatusError as e:
                    if _is_transient_5xx(e) and attempt + 1 < attempts:
                        last_exc = e
                        await asyncio.sleep(backoff * (attempt + 1))
                        continue
                    raise
                else:
                    if (
                        isinstance(result, str)
                        and _TRANSIENT_RESULT_RE.search(result)
                        and attempt + 1 < attempts
                    ):
                        await asyncio.sleep(backoff * (attempt + 1))
                        continue
                    return result
            # Defensive: shouldn't reach here, but if we do, surface the
            # last exception rather than returning silently.
            if last_exc is not None:
                raise last_exc
            return None
        return wrapper
    return deco
