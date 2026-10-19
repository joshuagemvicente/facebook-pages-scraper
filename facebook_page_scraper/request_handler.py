# facebook_page_scraper/request_handler.py

import curl_cffi
from selectolax.parser import HTMLParser
import json
import traceback
from datetime import datetime, timezone
from pathlib import Path

GRAPHQL_URL = "https://www.facebook.com/api/graphql/"
# Facebook's GraphQL error code for "Rate limit exceeded".
RATE_LIMIT_CODE = 1675004
# Response bodies in the error log are cut to this many characters.
LOGGED_BODY_CHARS = 4000


def graphql_form(query: dict, variables: dict, lsd: str) -> dict:
    """Form body for one logged-out Relay request."""
    return {
        "av": "0",
        "__user": "0",
        "__a": "1",
        "lsd": lsd,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": query["queryName"],
        "variables": json.dumps(variables),
        "doc_id": query["queryID"],
        "server_timestamps": "true",
    }


def graphql_lines(text: str) -> list[dict]:
    """A Relay response is one JSON object per line (first payload, streamed edges, deferred parts)."""
    text = text.removeprefix("for (;;);")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def graphql_errors(lines: list | None) -> list[dict]:
    """Errors of a response that carries no data. Facebook adds harmless errors to good responses too."""
    if not lines or any(isinstance(line, dict) and line.get("data") for line in lines):
        return []
    return [
        error
        for line in lines if isinstance(line, dict)
        for error in line.get("errors") or [] if isinstance(error, dict)
    ]


def is_rate_limited(lines: list | None) -> bool:
    return any(error.get("code") == RATE_LIMIT_CODE for error in graphql_errors(lines))


class ErrorLog:
    """Appends one JSON line per failure to path. No path means nothing is written."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None

    def write(self, kind: str, url: str, **details):
        if self.path is None:
            return
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind, "url": url}
        entry.update({key: value for key, value in details.items() if value is not None})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as e:
            print(f"Could not write the error log [{self.path}]: {e}")

    def failure(self, kind: str, url: str, response=None, error: Exception | None = None, **details):
        """Log a failed request with the response (status, headers, body) and the exception, if any."""
        if response is not None:
            details.update(
                status=response.status_code,
                headers=dict(response.headers.items()),
                body=(response.text or "")[:LOGGED_BODY_CHARS],
            )
        if error is not None:
            details.update(
                error_type=type(error).__name__,
                error=str(error),
                traceback="".join(traceback.format_exception(error)),
            )
        self.write(kind, url, **details)


def _request_failure(error: Exception, response) -> str:
    if response is not None and response.status_code >= 400:
        return "http_error"
    return "bad_response" if response is not None else "request_error"


def _graphql_result(log: ErrorLog, response, variables: dict, referer: str) -> list[dict]:
    lines = graphql_lines(response.text)
    errors = graphql_errors(lines)
    if errors:
        kind = "rate_limited" if is_rate_limited(lines) else "graphql_error"
        log.failure(kind, GRAPHQL_URL, response, page=referer, variables=variables, errors=errors)
    return lines


class RequestHandler:
    def __init__(self, proxy: str | None = None, error_log: str | Path | None = None):
        # One Chrome session: TLS, headers, and cookies stay consistent.
        # A custom or rotating user-agent disagrees with that fingerprint.
        self.session = curl_cffi.Session(impersonate="chrome", proxy=proxy)
        self.headers = {"accept-language": "en-US,en;q=0.9"}
        self.error_log = ErrorLog(error_log)

    def fetch_html(self, url: str):
        """Fetch url. Returns parsed HTML, or None when the request fails."""
        response = None
        try:
            response = self.session.get(url, headers=self.headers)
            response.raise_for_status()
            return HTMLParser(response.text)
        except Exception as e:
            print(f"Error fetching the page [{url}]: {e}")
            self.error_log.failure(_request_failure(e, response), url, response, e, method="GET")
            return None

    def post_graphql(self, query: dict, variables: dict, lsd: str, referer: str) -> list[dict] | None:
        """Run a Relay query taken from the page HTML. Returns the response lines, or None on failure."""
        response = None
        try:
            response = self.session.post(
                GRAPHQL_URL,
                headers={**self.headers, "x-fb-lsd": lsd, "origin": "https://www.facebook.com", "referer": referer},
                data=graphql_form(query, variables, lsd),
            )
            response.raise_for_status()
            return _graphql_result(self.error_log, response, variables, referer)
        except Exception as e:
            print(f"Error fetching posts [{referer}]: {e}")
            self.error_log.failure(
                _request_failure(e, response), GRAPHQL_URL, response, e,
                method="POST", page=referer, variables=variables,
            )
            return None

    def close(self):
        session = self.session
        self.session = None
        if session is not None:
            session.close()

    def find_json(self, html_content: HTMLParser, key_to_find: str) -> dict | None:
        """Return the first JSON script containing key_to_find, or None."""
        return next(self.iter_json(html_content, key_to_find), None)

    def iter_json(self, html_content: HTMLParser, key_to_find: str):
        """Yield every JSON script containing key_to_find."""
        if html_content is None:
            return
        for script in html_content.css('script[type="application/json"]'):
            script_text = script.text(strip=True)
            if key_to_find not in script_text:
                continue
            try:
                yield json.loads(script_text)
            except json.JSONDecodeError:
                continue


class AsyncRequestHandler(RequestHandler):
    """Same Chrome impersonation as RequestHandler, on an async session."""

    def __init__(self, proxy: str | None = None, error_log: str | Path | None = None):
        self.session = curl_cffi.AsyncSession(impersonate="chrome", proxy=proxy)
        self.headers = {"accept-language": "en-US,en;q=0.9"}
        self.error_log = ErrorLog(error_log)

    async def fetch_html(self, url: str):
        response = None
        try:
            response = await self.session.get(url, headers=self.headers)
            response.raise_for_status()
            return HTMLParser(response.text)
        except Exception as e:
            print(f"Error fetching the page [{url}]: {e}")
            self.error_log.failure(_request_failure(e, response), url, response, e, method="GET")
            return None

    async def post_graphql(self, query: dict, variables: dict, lsd: str, referer: str) -> list[dict] | None:
        response = None
        try:
            response = await self.session.post(
                GRAPHQL_URL,
                headers={**self.headers, "x-fb-lsd": lsd, "origin": "https://www.facebook.com", "referer": referer},
                data=graphql_form(query, variables, lsd),
            )
            response.raise_for_status()
            return _graphql_result(self.error_log, response, variables, referer)
        except Exception as e:
            print(f"Error fetching posts [{referer}]: {e}")
            self.error_log.failure(
                _request_failure(e, response), GRAPHQL_URL, response, e,
                method="POST", page=referer, variables=variables,
            )
            return None

    async def close(self):
        session = self.session
        self.session = None
        if session is not None:
            await session.close()
