#!/usr/bin/env python3
"""
Shared HTTP fetch helper for hf-timestd external-data sources.

The space-weather / ionosphere feeds hf-timestd depends on (NOAA SWPC,
GFZ, Lowell GIRO/DIDBase, …) are public services of varying reliability.
GIRO's DIDBase in particular has been observed to flap between HTTP 200,
503 (nginx overload) and Tomcat 404 within minutes. A naive single
``requests.get`` therefore drops data on every transient blip.

This module centralises one robust ``requests.Session`` policy so every
fetcher gets the same behaviour:

* bounded automatic retries with exponential backoff on connection
  errors and the transient 5xx / 429 status codes,
* a connect+read timeout on every request,
* a descriptive User-Agent so upstreams can identify (and contact) us.

It deliberately does NOT handle authentication — CDDIS/Earthdata
downloads keep their own credentialed session in ``cddis_auth`` because
that flow needs netrc-based basic auth and redirect handling.
"""

import logging
from typing import Optional

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 ships with requests; import defensively across versions
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3
    from requests.packages.urllib3.util.retry import Retry  # type: ignore

logger = logging.getLogger(__name__)

# Identify ourselves to upstreams. Mirrors the UA used by the shell
# fetchers (update-iri-indices.sh) so server logs attribute all
# hf-timestd traffic consistently.
DEFAULT_USER_AGENT = (
    "hf-timestd (https://github.com/mijahauan/hf-timestd)"
)

# (connect, read) seconds. Read is generous: GIRO/CDDIS can be slow.
DEFAULT_TIMEOUT = (10.0, 30.0)

# Status codes worth retrying — transient server / rate-limit faults.
_RETRY_STATUS = (429, 500, 502, 503, 504)


def build_session(
    total_retries: int = 3,
    backoff_factor: float = 1.0,
    user_agent: str = DEFAULT_USER_AGENT,
) -> requests.Session:
    """Create a ``requests.Session`` with bounded retry + backoff.

    Args:
        total_retries: Max retry attempts per request (in addition to the
            initial try) for connection errors and ``_RETRY_STATUS``.
        backoff_factor: Exponential backoff base; urllib3 sleeps
            ``backoff_factor * (2 ** (attempt - 1))`` seconds between
            tries (e.g. 1.0 → 0s, 1s, 2s, 4s).
        user_agent: ``User-Agent`` header sent on every request.

    Returns:
        A configured session. Callers should still pass ``timeout=`` to
        each request (or use :func:`get`, which defaults it).
    """
    retry = Retry(
        total=total_retries,
        connect=total_retries,
        read=total_retries,
        status=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=_RETRY_STATUS,
        # Retry idempotent GETs (the only verb the fetchers use).
        allowed_methods=frozenset({"GET", "HEAD"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": user_agent})
    return session


def get(
    url: str,
    *,
    session: Optional[requests.Session] = None,
    timeout=DEFAULT_TIMEOUT,
    **kwargs,
) -> requests.Response:
    """GET ``url`` with the shared retry/timeout policy.

    A one-shot convenience: if no ``session`` is supplied a transient one
    is built and closed around the call. Pass a long-lived ``session``
    when issuing many requests so connections (and the retry policy) are
    reused.

    Raises the underlying ``requests`` exception if all retries fail; the
    caller decides how to degrade.
    """
    if session is not None:
        return session.get(url, timeout=timeout, **kwargs)
    s = build_session()
    try:
        return s.get(url, timeout=timeout, **kwargs)
    finally:
        s.close()
