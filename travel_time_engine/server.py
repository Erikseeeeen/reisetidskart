from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import traceback

from .routing import (
    RoutingCancelled,
    _data_directory,
    _routing_libraries,
    compute_grid,
)


class _RequestTracker:
    """Track the newest request from each browser session."""

    def __init__(self):
        self._latest = {}
        self._lock = threading.Lock()

    def register(self, client_id, request_id):
        if not isinstance(client_id, str) or not isinstance(request_id, int):
            return lambda: False

        with self._lock:
            self._latest[client_id] = max(
                request_id,
                self._latest.get(client_id, request_id),
            )

        def superseded():
            with self._lock:
                return self._latest.get(client_id) != request_id

        return superseded


_REQUESTS = _RequestTracker()


class TravelTimeHandler(BaseHTTPRequestHandler):
    def send_json(self, status, payload):
        response = json.dumps(payload, allow_nan=False).encode()
        try:
            self.send_response(status)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Moving the marker aborts the obsolete browser request while R5
            # may still be finishing it.
            pass

    def do_POST(self):
        if self.path == "/travel-times":
            self.travel_times()
        else:
            self.send_json(404, {"error": "Not found"})
            return

    def travel_times(self):
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(content_length))
            cancelled = _REQUESTS.register(
                request.get("client_id"),
                request.get("request_id"),
            )
            minutes = compute_grid(
                request["origin"],
                request["bounds"],
                request["width"],
                request["height"],
                mode=request.get("mode", "public_transport"),
                departure_time=request.get("departure_time"),
                cancelled=cancelled,
            )
        except RoutingCancelled:
            self.send_json(409, {"error": "Superseded by a newer request"})
            return
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})
            return
        except Exception as error:
            traceback.print_exc()
            self.send_json(500, {"error": f"{type(error).__name__}: {error}"[:2000]})
            return

        self.send_json(200, {"minutes": minutes})


def main():
    """Validate the routing setup and start the local HTTP service."""

    _routing_libraries()
    data_directory = _data_directory()
    with ThreadingHTTPServer(("127.0.0.1", 8001), TravelTimeHandler) as server:
        print(f"R5 data: {data_directory}", flush=True)
        print("Travel-time engine: http://127.0.0.1:8001", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
