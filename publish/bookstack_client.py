"""
publish/bookstack_client.py — BookStack REST transport. No orchestration.

Stdlib urllib only, matching updater.py and generation/backends/
local_vision_backend.py. Adding `requests` would mean a requirements.txt line, a
build-venv install, and an autodoc.spec change for no functional gain — and a
missing module is only a WARNING to PyInstaller, so it would produce a build that
reports success and dies on launch. (httpx IS already in the spec's
hiddenimports as an Anthropic SDK transitive dep, but depending on it would
couple publishing to the anthropic package staying in the tree, and the shipped
config is provider: "local".)

Cost of stdlib-only: the multipart body for image upload is hand-built below.
Two traps in that, both of which produce a 422 from BookStack that does not name
the cause:
  - parts MUST be separated by CRLF, not LF;
  - the file part needs its own Content-Type header.

Auth: every request carries `Authorization: Token <id>:<secret>`.

API surface used (verified against the JCC instance's /api/docs, 2026-07-27):
  GET    /api/books?filter[name]=<name>
  POST   /api/books                {name, description}
  POST   /api/pages                {book_id, name, markdown, tags}
  PUT    /api/pages/<id>           {name, markdown, tags}
  POST   /api/image-gallery        multipart: type, uploaded_to, image, name
  DELETE /api/image-gallery/<id>
"""

import json
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

_CRLF = b"\r\n"


class BookStackError(RuntimeError):
    """Any BookStack API failure. Carries the HTTP status when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class BookStackClient:
    def __init__(
        self,
        base_url: str,
        token_id: str,
        token_secret: str,
        timeout_s: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._auth = f"Token {token_id}:{token_secret}"
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ #
    # Transport                                                            #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> dict:
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/json")
        if content_type:
            req.add_header("Content-Type", content_type)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            # BookStack returns its validation messages in the body, and they are
            # the only useful part of a 422 — surface them rather than "HTTP 422".
            raise BookStackError(
                f"{method} {path} failed: HTTP {exc.code} {exc.reason}"
                + (f"\n{detail}" if detail else ""),
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise BookStackError(
                f"{method} {path} failed: cannot reach {self.base_url} ({exc.reason})"
            ) from exc
        except TimeoutError as exc:
            raise BookStackError(
                f"{method} {path} timed out after {self.timeout_s}s"
            ) from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # Typically an HTML error page from a reverse proxy, i.e. the request
            # never reached BookStack. Say so rather than "invalid JSON".
            raise BookStackError(
                f"{method} {path} returned a non-JSON response "
                f"(is {self.base_url} really a BookStack instance?)"
            ) from exc

    def _json_request(self, method: str, path: str, payload: dict) -> dict:
        return self._request(
            method,
            path,
            body=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

    # ------------------------------------------------------------------ #
    # Books                                                                #
    # ------------------------------------------------------------------ #

    def find_book_by_name(self, name: str) -> int | None:
        """Exact-name lookup. Returns the book id, or None."""
        query = urllib.parse.urlencode({"filter[name]": name})
        data = self._request("GET", f"/api/books?{query}")
        for entry in data.get("data", []):
            # The filter is a LIKE match server-side, so confirm exactness here
            # rather than trusting the first row.
            if entry.get("name") == name:
                return int(entry["id"])
        return None

    def create_book(self, name: str, description: str = "") -> int:
        data = self._json_request(
            "POST", "/api/books", {"name": name, "description": description}
        )
        return int(data["id"])

    def ensure_book(self, name: str, description: str = "") -> int:
        existing = self.find_book_by_name(name)
        if existing is not None:
            return existing
        book_id = self.create_book(name, description)
        log.info("bookstack: created book %r (id=%s)", name, book_id)
        return book_id

    # ------------------------------------------------------------------ #
    # Pages                                                                #
    # ------------------------------------------------------------------ #

    def create_page(
        self,
        book_id: int,
        name: str,
        markdown: str,
        tags: list[dict] | None = None,
    ) -> dict:
        payload: dict = {
            "book_id": book_id,
            "name": name[:255],       # API limit; a longer name is a hard 422
            "markdown": markdown,
        }
        if tags:
            payload["tags"] = tags
        return self._json_request("POST", "/api/pages", payload)

    def update_page(
        self,
        page_id: int,
        markdown: str,
        name: str | None = None,
        tags: list[dict] | None = None,
    ) -> dict:
        payload: dict = {"markdown": markdown}
        if name:
            payload["name"] = name[:255]
        if tags:
            payload["tags"] = tags
        return self._json_request("PUT", f"/api/pages/{page_id}", payload)

    def get_page(self, page_id: int) -> dict:
        return self._request("GET", f"/api/pages/{page_id}")

    # ------------------------------------------------------------------ #
    # Images                                                               #
    # ------------------------------------------------------------------ #

    def upload_image(self, page_id: int, image_path: Path, name: str) -> dict:
        """
        Attach an image to a page's gallery. Returns the API's image record;
        the display URL is under "url" (older versions: "path").

        uploaded_to is REQUIRED by the API, which is what forces the whole
        create-page-then-upload-then-update sequence in bookstack_publisher.py.
        """
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        body, content_type = _multipart(
            fields={
                "type": "gallery",
                "uploaded_to": str(page_id),
                "name": name[:180],   # API limit
            },
            file_field="image",
            file_name=image_path.name,
            file_bytes=image_path.read_bytes(),
            file_mime=mime,
        )
        return self._request(
            "POST", "/api/image-gallery", body=body, content_type=content_type
        )

    def delete_image(self, image_id: int) -> None:
        self._request("DELETE", f"/api/image-gallery/{image_id}")


# ------------------------------------------------------------------ #
# Multipart encoding                                                   #
# ------------------------------------------------------------------ #

def _multipart(
    fields: dict[str, str],
    file_field: str,
    file_name: str,
    file_bytes: bytes,
    file_mime: str,
) -> tuple[bytes, str]:
    """
    Build a multipart/form-data body. Returns (body, content_type_header).

    CRLF line endings are mandatory per RFC 7578 — Laravel's parser (BookStack's
    framework) rejects LF-separated parts with a validation error that blames the
    'image' field rather than the encoding, which is a genuinely misleading
    symptom. The boundary is a fixed literal because it only has to not appear in
    the payload, and PNG bytes containing this exact ASCII string is not a real
    risk.
    """
    boundary = "----AutoDocFormBoundary7MA4YWxkTrZu0gW"
    out = bytearray()

    for key, value in fields.items():
        out += b"--" + boundary.encode() + _CRLF
        out += f'Content-Disposition: form-data; name="{key}"'.encode() + _CRLF
        out += _CRLF
        out += str(value).encode("utf-8") + _CRLF

    out += b"--" + boundary.encode() + _CRLF
    out += (
        f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"'
    ).encode() + _CRLF
    out += f"Content-Type: {file_mime}".encode() + _CRLF
    out += _CRLF
    out += file_bytes + _CRLF

    out += b"--" + boundary.encode() + b"--" + _CRLF
    return bytes(out), f"multipart/form-data; boundary={boundary}"
