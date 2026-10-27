import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"authorization": self.headers.get("Authorization"),
                           "proto": self.headers.get("X-Forwarded-Proto"),
                           "path": self.path, "backend": os.environ.get("RESPONSE_MARKER", "first")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
