from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import traceback

from .dummy import _data_directory, _routing_libraries, compute_grid


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        response = json.dumps(payload, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        try:
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionResetError):
            # Moving the marker aborts the obsolete browser request while R5
            # may still be finishing it.
            pass

    def do_POST(self):
        if self.path != "/travel-times":
            self.send_json(404, {"error": "Not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(content_length))
            minutes = compute_grid(
                request["origin"],
                request["bounds"],
                request["width"],
                request["height"],
                mode=request.get("mode", "public_transport"),
                departure_time=request.get("departure_time"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        except Exception as error:
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(error).__name__}: {error}"[:2000]})
            return

        self.send_json(200, {"minutes": minutes})


if __name__ == "__main__":
    _routing_libraries()
    data_directory = _data_directory()
    with ThreadingHTTPServer(("127.0.0.1", 8001), Handler) as server:
        print(f"R5 data: {data_directory}", flush=True)
        print("Travel-time engine: http://127.0.0.1:8001", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
