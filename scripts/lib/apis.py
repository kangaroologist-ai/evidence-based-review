from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

import httpx

from . import project

EMAIL = "kangaroologician@gmail.com"


def _load_key_file(env_var: str, state_filename: str) -> str | None:
    env = os.getenv(env_var)
    if env:
        return env.strip() or None
    key_path = project.project_root() / "state" / state_filename
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
# E5: bound the connection pool to the worker budget (plan §5.2 DAEMON_MAX_
# WORKERS) instead of httpx's default 100/20. With burst>1 + a shared executor
# the in-flight count is ~max_workers; capping connections here keeps a runaway
# nested-pool scenario from exhausting sockets rather than queuing at the gate.
_MAX_HTTP_CONNECTIONS = 32
_CLIENT = httpx.Client(
    headers={"User-Agent": f"health-review/1.0 (mailto:{EMAIL})"},
    timeout=30.0,
    follow_redirects=True,
    limits=httpx.Limits(
        max_connections=_MAX_HTTP_CONNECTIONS,
        max_keepalive_connections=_MAX_HTTP_CONNECTIONS // 2,
    ),
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
    key = _active_openalex_key()
    if "api.openalex.org" in url and key:
        merged: QueryParams = dict(params or {})
        merged.setdefault("api_key", key)
        return merged
    return params


def _augment_headers(url: str) -> dict[str, str] | None:
    """Inject host-specific auth headers. Semantic Scholar uses x-api-key."""
    key = _active_semantic_scholar_key()
    if "api.semanticscholar.org" in url and key:
        return {"x-api-key": key}
    return None


def _cache_dir_candidates() -> tuple[pathlib.Path, ...]:
    env_dir = os.getenv("HEALTH_REVIEW_CACHE_DIR")
    if env_dir:
        return (pathlib.Path(env_dir),)
    return (
        pathlib.Path.home() / ".cache" / "health-review",
        project.project_root() / ".runtime-cache" / "health-review",
        pathlib.Path("/tmp") / "health-review-cache",
    )


def _init_cache_dir() -> pathlib.Path:
    for candidate in _cache_dir_candidates():
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    raise RuntimeError("unable to initialize cache directory")


CACHE_DIR = _init_cache_dir()


# ---- E3 B12-bis: per-request key / cache override (daemon multi-topic) ------
# The module constants above (OPENALEX_API_KEY / SEMANTIC_SCHOLAR_API_KEY /
# CACHE_DIR) are frozen at import. In the standalone CLI that's correct — each
# invocation is a fresh process whose env they reflect. In the long-lived
# daemon (E3) they'd pin the FIRST request's values forever, so two topics with
# different keys would silently share one key. The daemon's dispatch layer sets
# this override from the forwarded whitelist env (plan §2.3); the three readers
# below consult it and fall back to the module constants when unset (so CLI
# behaviour is unchanged — the local path never sets it).
#
# WHY A PLAIN MODULE GLOBAL, NOT a ContextVar: the tools' actual HTTP happens in
# `--parallel` ThreadPoolExecutor WORKER threads, and concurrent.futures does
# NOT copy the submitter's context into workers — a ContextVar set in the
# dispatch thread is invisible there, so the override would silently not apply
# to the very calls (genealogy/fetch) that need it. The daemon serializes all
# dispatch under _DISPATCH_LOCK (one run() at a time, §2.7-a), so a process
# global is set → workers spawn+join → reset, with thread start/join ordering
# the write before any worker read and the reset after every worker join. No
# lock needed on the global itself; correctness rests on that serialization.
#
# ⚠ E5 (when serialization is relaxed to overlap requests) this global races
# across dispatches — at that point each `executor.submit` must run under
# `contextvars.copy_context().run(...)` (or an executor initializer) so a per-
# request key reaches its own workers without a shared global. See plan §5.2.
_REQUEST_OVERRIDE: dict[str, str] | None = None


def set_request_ctx(env: dict[str, str] | None) -> dict[str, str] | None:
    """Install per-request key/cache overrides; returns the previous override so
    reset_request_ctx can restore it. Call only while holding the daemon's
    _DISPATCH_LOCK (see the module note above). ``env`` is the whitelisted-env
    dict (OPENALEX_API_KEY / SEMANTIC_SCHOLAR_API_KEY / HEALTH_REVIEW_CACHE_DIR);
    a cache dir is normalised to an absolute, created path once here so per-call
    _cache_key stays cheap and immune to the request's later os.chdir."""
    global _REQUEST_OVERRIDE
    previous = _REQUEST_OVERRIDE
    if env:
        env = dict(env)
        cache = env.get("HEALTH_REVIEW_CACHE_DIR")
        if cache:
            path = pathlib.Path(cache).expanduser().resolve()
            path.mkdir(parents=True, exist_ok=True)
            env["HEALTH_REVIEW_CACHE_DIR"] = str(path)
    _REQUEST_OVERRIDE = env or None
    return previous


def reset_request_ctx(previous: dict[str, str] | None) -> None:
    global _REQUEST_OVERRIDE
    _REQUEST_OVERRIDE = previous


def _ctx_override(name: str) -> str | None:
    override = _REQUEST_OVERRIDE
    if override is None:
        return None
    return override.get(name)


def _active_openalex_key() -> str | None:
    override = _ctx_override("OPENALEX_API_KEY")
    if override is not None:
        return override.strip() or None
    return OPENALEX_API_KEY


def _active_semantic_scholar_key() -> str | None:
    override = _ctx_override("SEMANTIC_SCHOLAR_API_KEY")
    if override is not None:
        return override.strip() or None
    return SEMANTIC_SCHOLAR_API_KEY


def _active_cache_dir() -> pathlib.Path:
    override = _ctx_override("HEALTH_REVIEW_CACHE_DIR")
    if override:  # already absolute + created by set_request_ctx
        return pathlib.Path(override)
    return CACHE_DIR


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


# ---- E1: central per-host token bucket (HostGate) --------------------------
# Replaces _throttle's interval lock as the single choke point for ALL HTTP
# (get_json/text/bytes → _get_response → _acquire). burst=1 is byte-equivalent
# to the old per-host interval throttle (docs/orchestrator_implementation_plan
# .md §3.4). Set `_GATE = None` to revert to the legacy `_throttle` path (kept
# intact above for fallback).


@dataclass(frozen=True)
class HostPolicy:
    rate: float   # tokens/sec, long-run average
    burst: float  # bucket capacity = allowed instantaneous concurrency peak


# E5 (B15): widen burst on the polite-pool hosts so a round's verify/fetch/
# genealogy can issue a small concurrent fleet across hosts instead of strictly
# serializing each (plan §3.1 / §7). The post-429 refill cap (_Bucket.penalize +
# acquire's _recovering branch) keeps a burst from re-flooding a host that just
# rate-limited us. rate=8 (< the 10/s default) is DELIBERATE, not a typo: we
# trade a slightly lower long-run average for burst headroom — 4 concurrent is
# well under the OpenAlex/CrossRef/EuropePMC polite-pool ceilings. www.ebi.ac.uk
# is EuropePMC, fetch's main abstract/XML host (fetch.EUPMC_BASE) — without an
# entry it would fall to the serial burst=1 default and the whole chain-fetch
# phase would lose E5's parallelism. api.labs.crossref.org (retraction-watch,
# a _FLAKY_HOSTS single-shot) is intentionally NOT here: exact-host matching
# leaves it on the burst=1 default, which is correct for a flaky single-shot.
#
# Rollback (HEALTH_REVIEW_GATE_CONSERVATIVE=1) forces every host back to burst=1
# (the E1 parity config, plan §7 "配置回小"). ⚠ This env is read ONCE at import
# (_GATE_POLICIES below freezes the buckets), so in the long-lived daemon it
# only takes effect after a restart — call rebuild_gate() or let the daemon
# idle-exit (≤5 min) and re-fork. In the per-invocation CLI it's a fresh process
# so the env applies immediately.
_GATE_BURST_POLICIES: dict[str, HostPolicy] = {
    "api.openalex.org": HostPolicy(rate=8.0, burst=4.0),
    "api.crossref.org": HostPolicy(rate=8.0, burst=4.0),
    "www.ebi.ac.uk": HostPolicy(rate=8.0, burst=4.0),
}


def _build_gate_policies() -> dict[str, HostPolicy]:
    # S2 caps at 1 req/s cumulative → rate=1, burst=1 (never widened).
    policies: dict[str, HostPolicy] = {
        host: HostPolicy(rate=1.0 / interval, burst=1.0)
        for host, interval in _HOST_MIN_INTERVAL.items()
    }
    if os.getenv("HEALTH_REVIEW_GATE_CONSERVATIVE") != "1":
        policies.update(_GATE_BURST_POLICIES)
    return policies


#   DEFAULT rate = 1/0.1s = 10/s ⇔ old 0.1s;  S2 rate = 1/1.0s = 1/s ⇔ old 1.0s.
_GATE_DEFAULT = HostPolicy(rate=1.0 / _DEFAULT_MIN_INTERVAL_SECONDS, burst=1.0)
_GATE_POLICIES: dict[str, HostPolicy] = _build_gate_policies()


class _Bucket:
    """Lazy-refill token bucket guarded by a Condition — no background thread,
    and never sleeps while holding the lock (cv.wait releases it)."""

    def __init__(self, rate: float, burst: float) -> None:
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        # monotonic(): immune to NTP steps (legacy _throttle used time.time()).
        self.updated = time.monotonic()
        # E5: set by penalize(), cleared on the first post-penalty grant — caps
        # that grant to a single token so a 429 isn't immediately answered with
        # a fresh burst=N fleet (see acquire()).
        self._recovering = False
        self.cv = threading.Condition()

    def acquire(self) -> None:
        with self.cv:
            while True:
                now = time.monotonic()
                refilled = min(self.burst, self.tokens + (now - self.updated) * self.rate)
                if self._recovering:
                    # E5: hand back at most ONE token after a 429, not a full
                    # burst — a burst refill here would release a fresh fleet
                    # straight into the host that just rate-limited us. Normal
                    # rate-ramp toward `burst` resumes once this token is taken.
                    refilled = min(refilled, 1.0)
                self.tokens = refilled
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self._recovering = False
                    return
                deficit = 1.0 - self.tokens
                self.cv.wait(timeout=max(deficit / self.rate, 0.0))

    def penalize(self, penalty_seconds: float) -> None:
        """429 → empty the bucket and push `updated` into the future so every
        subsequent acquire blocks ~penalty_seconds (global per-host slowdown).
        The negative refill that results is self-consistent: the next acquire's
        deficit/rate wait covers exactly the penalty window.

        `max(self.updated, …)` so a small concurrent penalty can't shorten a
        larger one already in effect. ``_recovering`` (E5) caps the first
        post-penalty grant to one token so a burst>1 bucket that sat idle
        through the penalty doesn't refill to `burst` and release a fresh
        car-fleet into the just-throttled host."""
        with self.cv:
            self.tokens = 0.0
            self.updated = max(
                self.updated, time.monotonic() + max(penalty_seconds, 1.0)
            )
            self._recovering = True
            self.cv.notify_all()


class HostGate:
    """Per-host token buckets behind one map. Each host owns its own Condition,
    so a crossref wait never blocks an openalex acquire (preserves the legacy
    per-host isolation)."""

    def __init__(self, policies: dict[str, HostPolicy], default: HostPolicy) -> None:
        self._policies = policies
        self._default = default
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, host: str) -> _Bucket:
        with self._lock:
            bucket = self._buckets.get(host)
            if bucket is None:
                policy = self._policies.get(host, self._default)
                bucket = self._buckets[host] = _Bucket(policy.rate, policy.burst)
            return bucket

    def acquire(self, host: str) -> None:
        self._bucket(host).acquire()

    def penalize(self, host: str, penalty_seconds: float) -> None:
        self._bucket(host).penalize(penalty_seconds)


# Module singleton (process-wide; verify's verify_pool/fetch_pool share it).
# None → fall back to the legacy _throttle path.
_GATE: HostGate | None = HostGate(_GATE_POLICIES, _GATE_DEFAULT)


def rebuild_gate() -> None:
    """Rebuild _GATE from the current env (re-reads HEALTH_REVIEW_GATE_
    CONSERVATIVE). The policy map is frozen at import, so a long-lived daemon
    must call this — e.g. on SIGHUP, or per-dispatch when the env changed — for
    a conservative-mode rollback to take effect without a restart. In-flight
    buckets are discarded; their pending penalties reset (acceptable for a
    deliberate config flip). A no-op when the gate is disabled (_GATE is None)."""
    global _GATE_POLICIES, _GATE
    if _GATE is None:
        return
    _GATE_POLICIES = _build_gate_policies()
    _GATE = HostGate(_GATE_POLICIES, _GATE_DEFAULT)


def _acquire(url: str) -> None:
    if _GATE is not None:
        _GATE.acquire(_host_of(url))
    else:
        _throttle(url)


def _cache_key(url: str, params: QueryParams | None, suffix: str) -> pathlib.Path:
    payload = url + "?" + json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()
    return _active_cache_dir() / f"{digest}.{suffix}"


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
            _acquire(url)
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
        if response.status_code == 429:
            # Quota signal → slow the WHOLE host. Penalize the host bucket
            # UNCONDITIONALLY (even on the last attempt / flaky single-shot
            # hosts): it only mutates bucket state and benefits every later
            # request on that host. The loop-top _acquire then blocks the
            # triggering thread too, unifying self-penalty + global penalty
            # (plan §3.3). Without the gate, fall back to a backoff sleep — but
            # only when we'll actually retry (no point sleeping otherwise).
            last_error = "http 429"
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            penalty = (
                retry_after
                if retry_after is not None
                else _backoff_seconds(attempt, None)
            )
            if _GATE is not None:
                _GATE.penalize(_host_of(url), penalty)
            elif attempt + 1 < attempts:
                time.sleep(penalty)
            continue
        if response.status_code in {500, 502, 503, 504}:
            # Transient SERVER error, not a quota signal → exponential backoff;
            # do NOT penalize the host bucket (would wrongly throttle the host
            # for an upstream hiccup).
            last_error = f"http {response.status_code}"
            if attempt + 1 < attempts:
                time.sleep(_backoff_seconds(attempt, None))
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


def _recheck_fresh() -> bool:
    """N11/§0.6.k: finalize's撤稿/EoC realtime recheck must NOT read the N3 disk cache —
    a DOI retracted within the 30-day TTL window would otherwise return a stale
    'not retracted' payload. finalize sets HEALTH_REVIEW_RECHECK_FRESH=1 on the recheck
    subprocess (inherited down through recheck.py → verify.py → here); it forces a live
    fetch and overwrites the cache entry, bypassing BOTH positive and negative caching."""
    return os.environ.get("HEALTH_REVIEW_RECHECK_FRESH") == "1"


def get_json_with_status(
    url: str,
    params: QueryParams | None = None,
    ttl_days: int = 30,
) -> tuple[JsonFetchStatus, dict[str, object] | None]:
    cache_path = _cache_key(url, params, "json")
    if cache_path.exists() and not _recheck_fresh():
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
    if cache_path.exists() and not _recheck_fresh():
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
