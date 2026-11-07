import json
import os
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import urlopen
from urllib.error import HTTPError

import psycopg


def emit(**event):
    event["at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(event), flush=True)


def connect(role):
    return psycopg.connect(
        host="db", dbname="checkout_lab", user=role, password=f"lab-{role}",
        connect_timeout=3, autocommit=True,
    )


class Checkout(BaseHTTPRequestHandler):
    slots = None

    def do_GET(self):
        if self.path != "/probe":
            self.send_error(404)
            return
        try:
            with self.slots, connect("checkout") as db:
                (result,) = db.execute("SELECT id FROM checkout_probe WHERE id = 1").fetchone()
            status, detail = 200, f"PostgreSQL returned row {result}"
        except psycopg.Error as exc:
            status, detail = 503, str(exc).strip()
        emit(kind="checkout", status=status, detail=detail)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": status, "detail": detail}).encode())

    def log_message(self, *_):
        pass


def worker():
    pool_max = int(os.environ["WORKER_POOL_MAX"])
    pool = []
    try:
        while True:
            if len(pool) < pool_max:
                try:
                    pool.append(connect("worker"))
                    emit(kind="worker_open", connections=len(pool), pool_max=pool_max)
                    continue
                except psycopg.Error as exc:
                    emit(kind="worker_rejected", connections=len(pool), detail=str(exc).strip())
            time.sleep(1)
    finally:
        for db in pool:
            db.close()


def probe():
    try:
        with urlopen("http://127.0.0.1:8080/probe", timeout=5) as response:
            print(response.read().decode())
    except HTTPError as exc:
        print(exc.read().decode())


def ready():
    for _ in range(30):
        try:
            with socket.create_connection(("127.0.0.1", 8080), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise SystemExit("Checkout HTTP server did not start")


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "checkout":
        Checkout.slots = threading.BoundedSemaphore(int(os.environ["CHECKOUT_MAX"]))
        ThreadingHTTPServer(("0.0.0.0", 8080), Checkout).serve_forever()
    elif mode == "worker":
        worker()
    elif mode == "probe":
        probe()
    elif mode == "ready":
        ready()
