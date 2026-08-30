#!/usr/bin/env python3
"""
Tiny server for the label-reader page.

The browser cannot call Anthropic directly (CORS + you should not
put an API key in front-end code in production). This file:

  1. Serves temp.html
  2. Accepts photos from the page
  3. Forwards them to Claude's Messages API
  4. Sends Claude's text reply back to the page
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HTML_PATH = Path(__file__).resolve().parent / "temp.html"
HOST = "127.0.0.1"
PORT = 8765
MODEL = "claude-sonnet-4-6"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# This is the instruction Claude sees along with the photos.
PROMPT = """Look at these product photos and read the labels.

List what you can actually see: product name, brand, ingredients,
allergens, net quantity, barcode, nutrition, dates, warnings.
If something is unreadable, say so. Do not invent details."""


def call_claude(api_key, images):
    """Build a Messages API request and return Claude's text reply."""

    # A Claude message is a list of blocks: images first, then the prompt.
    content = []
    for image in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image["media_type"],
                "data": image["data"],
            },
        })
    content.append({"type": "text", "text": PROMPT})

    body = {
        "model": MODEL,
        "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": content},
        ],
    }

    request = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
    )

    with urllib.request.urlopen(request, timeout=120) as response:
        result = json.loads(response.read().decode("utf-8"))

    # The reply is a list of blocks. We want the first text block.
    for block in result.get("content", []):
        if block.get("type") == "text":
            return block["text"]
    return "(Claude returned no text)"


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path in ("/", "/index.html", "/temp.html"):
            self._send(200, HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, json.dumps({"error": "Not found"}), "application/json")

    def do_POST(self):
        if self.path != "/analyze":
            self._send(404, json.dumps({"error": "Not found"}), "application/json")
            return

        length = int(self.headers.get("Content-Length", "0"))
        data = json.loads(self.rfile.read(length))

        api_key = (data.get("api_key") or "").strip()
        images = data.get("images") or []

        if not api_key:
            self._send(400, json.dumps({"error": "Missing API key"}), "application/json")
            return
        if not images:
            self._send(400, json.dumps({"error": "No images"}), "application/json")
            return

        try:
            text = call_claude(api_key, images)
            self._send(200, json.dumps({"text": text}), "application/json")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(detail)["error"]["message"]
            except (KeyError, json.JSONDecodeError):
                message = detail or str(exc)
            self._send(exc.code, json.dumps({"error": message}), "application/json")

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    print("Open http://%s:%s" % (HOST, PORT), flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
