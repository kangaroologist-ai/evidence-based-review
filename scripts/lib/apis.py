from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import threading
import time
from typing import Literal, TypeAlias, cast

import httpx

from . import project


def _load_key_file(env_var: str, state_filename: str) -> str | None:
    env = os.getenv(env_var)
    if env:
        return env.strip() or None
    try:
        key_path = project.project_root() / "state" / state_filename
    except RuntimeError:
        # Workspace not configured yet (EBR pre-setup). Keys + email are all
        # optional — fall back to the shared pool / neutral polite-pool identity.
        return None
    if key_path.exists():
        text = key_path.read_text(encoding="utf-8").strip()
        return text or None
    return None


def _load_openalex_key() -> str | None:
    return _load_key_file("OPENALEX_API_KEY", "openalex_api_key")


def _load_semantic_scholar_key() -> str | None:
    return _load_key_file("SEMANTIC_SCHOLAR_API_KEY", "semantic_scholar_api_key")


OPENALEX_API_KEY = _load_openalex_key()
SEMANTIC_SCHOLAR_API_KEY = _load_semantic_scholar_key()
# Polite-pool identity, configurable so third-party installs don't impersonate
# the original author: env EBR_EMAIL > <workspace>/state/email > neutral default.
EMAIL = _load_key_file("EBR_EMAIL", "email") or "ebr-tools@users.noreply.example.com"
_DEFAULT_MIN_INTERVAL_SECONDS = 0.1
# Per-host override. Semantic Scholar issues keys with a 1 req/s cumulative
# cap across all endpoints; overshooting returns 429.
_HOST_MIN_INTERVAL: dict[str, float] = {
    "api.semanticscholar.org": 1.0,
}
# Per-host throttle: each host gets its own window. Crossref staying warm
# in cache should never block an OpenAlex /sources/{id} call.
_LAST_REQUEST_AT: dict[str, float] = {}
_THROTTLE_LOCK = threading.Lock()
ScalarParam: TypeAlias = str | int | float | bool | None
ParamValue: TypeAlias = ScalarParam | list[ScalarParam] | tuple[ScalarParam, ...]
QueryParams: TypeAlias = dict[str, ParamValue]
JsonFetchStatus: TypeAlias = Literal["ok", "missing", "transient"]
_CLIENT = httpx.Client(
    headers={"User-Agent": f"EBR/1.0 (mailto:{EMAIL})"},
    timeout=30.0,
    follow_redirects=True,
)


def _host_of(url: str) -> str:
    # urllib.parse is overkill for this; the scheme://host/path shape is fixed.
    after_scheme = url.split("://", 1)[-1]
    return after_scheme.split("/", 1)[0].lower()


def with_mailto(params: QueryParams | None = None) -> QueryParams:
    """Return params with `mailto=` injected — Crossref polite-pool requirement."""
    merged: QueryParams = dict(params or {})
    merged.setdefault("mailto", EMAIL)
    return merged


def _augment_params(url: str, params: QueryParams | None) -> QueryParams | None:
    """Inject host-specific auth / polite-pool params before sending."""
    if "api.openalex.org" in url and OPENALEX_API_KEY:
        merged: QueryParams = dict(params or {})
        merged.setdefault("api_key", OPENALEX_API_KEY)
        return merged
    return params


def _augment_headers(url: str) -> dict[str, str] | None:
    """Inject host-specific auth headers. Semantic Scholar uses x-api-key."""
    if "api.semanticscholar.org" in url and SEMANTIC_SCHOLAR_API_KEY:
        return {"x-api-key": SEMANTIC_SCHOLAR_API_KEY}
    return None


def _cache_dir_candidates() -> tuple[pathlib.Path, ...]:
    env_dir = os.getenv("HEALTH_REVIEW_CACHE_DIR")
    if env_dir:
        return (pathlib.Path(env_dir),)
    candidates = [pathlib.Path.home() / ".cache" / "health-review"]
    try:
        # Workspace-local cache, only when a workspace is configured. apis is
        # imported by setup.py (which runs BEFORE HEALTH_REVIEW_ROOT exists), so
        # this must not crash when project_root() can't resolve yet.
        candidates.append(project.project_root() / ".runtime-cache" / "health-review")
    except RuntimeError:
        pass
    candidates.append(pathlib.Path("/tmp") / "health-review-cache")
    return tuple(candidates)


def _init_cache_dir() -> pathlib.Path:
    for candidate in _cache_dir_candidates():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    raise RuntimeError("unable to initialize cache directory")


CACHE_DIR = _init_cache_dir()


def _throttle(url: str) -> None:
    host = _host_of(url)
    min_interval = _HOST_MIN_INTERVAL.get(host, _DEFAULT_MIN_INTERVAL_SECONDS)
    sleep_for = 0.0
    with _THROTTLE_LOCK:
        now = time.time()
        last = _LAST_REQUEST_AT.get(host, 0.0)
        gap = now - last
        if gap < min_interval:
            sleep_for = min_interval - gap
            _LAST_REQUEST_AT[host] = now + sleep_for
        else:
            _LAST_REQUEST_AT[host] = now
    if sleep_for > 0:
        time.sleep(sleep_for)


def _cache_key(url: str, params: QueryParams | None, suffix: str) -> pathlib.Path:
    payload = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.{suffix}"


_FLAKY_HOSTS = ("api.labs.crossref.org",)


def _is_flaky(url: str) -> bool:
    return any(host in url for host in _FLAKY_HOSTS)


def _request_timeout(url: str) -> float:
    return 5.0 if _is_flaky(url) else 30.0


def _max_attempts(url: str) -> int:
    # Flaky endpoints get one shot — we don't want N×timeout per call.
    return 1 if _is_flaky(url) else 3


class TransientError(Exception):
    """Raised when a request fails after all retries due to transient causes
    (network error, timeout, 429, 5xx). Callers must NOT cache this as a
    negative result — the upstream may recover within minutes."""


_BACKOFF_CAP_SECONDS = 30.0
_DEFAULT_BACKOFF_BASE_SECONDS = 1.0


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Spec allows seconds (int/float) or HTTP
    date; servers in practice return seconds. Returns None on parse failure."""
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        return None


def _backoff_seconds(attempt: int, retry_after: float | None) -> float:
    """Sleep duration before the next retry. Honors Retry-After when the
    server provides one; otherwise exponential 1s/2s/4s/... capped at 30s."""
    if retry_after is not None:
        return min(retry_after, _BACKOFF_CAP_SECONDS)
    return min(_DEFAULT_BACKOFF_BASE_SECONDS * (2**attempt), _BACKOFF_CAP_SECONDS)


def _get_response(
    url: str,
    params: QueryParams | None = None,
) -> httpx.Response | None:
    """Returns:
        httpx.Response  — success
        None            — definitive negative (404 or non-retryable 4xx)
    Raises:
        TransientError  — all retries exhausted on transient failures
                          (network, timeout, 429, 5xx). Caller should NOT
                          write a negative-cache sentinel.
    """
    timeout = _request_timeout(url)
    attempts = _max_attempts(url)
    effective_params = _augment_params(url, params)
    effective_headers = _augment_headers(url)
    last_error: str = "no attempt made"
    for attempt in range(attempts):
        try:
            _throttle(url)
            response = _CLIENT.get(
                url,
                params=effective_params,
                headers=effective_headers,
                timeout=timeout,
            )
        except httpx.RequestError as exc:
            last_error = f"network: {type(exc).__name__}: {exc}"
            if attempt + 1 < attempts:
                time.sleep(_backoff_seconds(attempt, None))
            continue

        if response.status_code == 404:
            return None  # definitive: resource does not exist
        if response.status_code in {429, 500, 502, 503, 504}:
            last_error = f"http {response.status_code}"
            if attempt + 1 < attempts:
                retry_after = (
                    _parse_retry_after(response.headers.get("Retry-After"))
                    if response.status_code == 429
                    else None
                )
                time.sleep(_backoff_seconds(attempt, retry_after))
            continue
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            # Non-404 4xx (401/403/410 etc). Treat as definitive — auth/config
            # problems do not heal by themselves and re-trying every call wastes
            # quota. If the user fixes credentials, they can manually clear
            # the cache directory.
            return None
        return response

    raise TransientError(f"exhausted retries for {url} ({last_error})")


_NEGATIVE_CACHE_TTL_DAYS = 7
_NEGATIVE_SENTINEL = '{"__missing__": true}'


def _atomic_write_text(path: pathlib.Path, text: str) -> None:
    tag = f".{os.getpid()}.{threading.get_ident()}.tmp"
    temp_path = path.with_suffix(path.suffix + tag)
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def _atomic_write_bytes(path: pathlib.Path, data: bytes) -> None:
    tag = f".{os.getpid()}.{threading.get_ident()}.tmp"
    temp_path = path.with_suffix(path.suffix + tag)
    temp_path.write_bytes(data)
    os.replace(temp_path, path)


def get_json(
    url: str,
    params: QueryParams | None = None,
    ttl_days: int = 30,
) -> dict[str, object] | None:
    status, data = get_json_with_status(url, params=params, ttl_days=ttl_days)
    if status == "ok":
        return data
    return None


def get_json_with_status(
    url: str,
    params: QueryParams | None = None,
    ttl_days: int = 30,
) -> tuple[JsonFetchStatus, dict[str, object] | None]:
    cache_path = _cache_key(url, params, "json")
    if cache_path.exists():
        age_seconds = time.time() - cache_path.stat().st_mtime
        text = cache_path.read_text(encoding="utf-8")
        is_negative = text.strip() == _NEGATIVE_SENTINEL
        ttl = _NEGATIVE_CACHE_TTL_DAYS if is_negative else ttl_days
        if age_seconds < ttl * 86400:
            if is_negative:
                return "missing", None
            return "ok", cast(dict[str, object], json.loads(text))

    try:
        response = _get_response(url, params=params)
    except TransientError as exc:
        # Do NOT write a negative-cache entry — the next call should retry.
        print(f"[WARN] {exc}", file=sys.stderr)
        return "transient", None
    if response is None:
        # Definitive negative (404 / non-retryable 4xx) — safe to cache.
        _atomic_write_text(cache_path, _NEGATIVE_SENTINEL)
        return "missing", None

    data = cast(dict[str, object], response.json())
    _atomic_write_text(
        cache_path,
        json.dumps(data, ensure_ascii=False, indent=2),
    )
    return "ok", data


_NEGATIVE_TEXT_SENTINEL = "\x00__missing__\x00"


def get_text(
    url: str,
    params: QueryParams | None = None,
    ttl_days: int = 30,
) -> str | None:
    cache_path = _cache_key(url, params, "txt")
    if cache_path.exists():
        age_seconds = time.time() - cache_path.stat().st_mtime
        text = cache_path.read_text(encoding="utf-8")
        is_negative = text == _NEGATIVE_TEXT_SENTINEL
        ttl = _NEGATIVE_CACHE_TTL_DAYS if is_negative else ttl_days
        if age_seconds < ttl * 86400:
            return None if is_negative else text

    try:
        response = _get_response(url, params=params)
    except TransientError as exc:
        print(f"[WARN] {exc}", file=sys.stderr)
        return None
    if response is None:
        _atomic_write_text(cache_path, _NEGATIVE_TEXT_SENTINEL)
        return None

    _atomic_write_text(cache_path, response.text)
    return response.text


def get_bytes(url: str) -> bytes | None:
    cache_path = _cache_key(url, None, "bin")
    if cache_path.exists():
        return cache_path.read_bytes()

    try:
        response = _get_response(url)
    except TransientError as exc:
        print(f"[WARN] {exc}", file=sys.stderr)
        return None
    if response is None:
        return None

    _atomic_write_bytes(cache_path, response.content)
    return response.content
