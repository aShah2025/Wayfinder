"""
web.py: one careful way to call outside web services (Valhalla, Photon, Overpass).

Wayfinder uses FREE public servers. They're great, but sometimes they're slow,
say "too many requests" (HTTP 429), or have a hiccup (HTTP 500+). Instead of
failing right away, we wait a moment and try again, up to 3 times.
"""
import asyncio

import httpx

HEADERS = {"User-Agent": "Wayfinder (ImpactHack student project)"}


class ServiceUnavailable(Exception):
    """Raised when a web service still fails after every retry."""


async def request_json(method, url, *, json=None, data=None, params=None,
                       timeout=20, attempts=3):
    """
    Call a web service and return (status_code, answer_as_python_data).
    Retries on timeouts, connection problems, rate limits (429), and server errors (5xx).
    Normal "no" answers (like Valhalla's 400 "no route found") are returned, not retried.
    """
    last_problem = "unknown error"
    for attempt in range(attempts):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method, url, json=json, data=data, params=params, headers=HEADERS
                )
            if response.status_code == 429 or response.status_code >= 500:
                last_problem = f"HTTP {response.status_code}"
            else:
                try:
                    return response.status_code, response.json()
                except ValueError:  # the server sent back something that isn't JSON
                    last_problem = "the server sent an unreadable answer"
        except httpx.TimeoutException:
            last_problem = "it took too long to answer"
        except httpx.TransportError:
            last_problem = "couldn't connect"

        if attempt < attempts - 1:
            await asyncio.sleep(1 + attempt * 1.5)  # wait 1s, then 2.5s, before retrying

    print(f"[web] {url} failed after {attempts} tries: {last_problem}")
    raise ServiceUnavailable(last_problem)
