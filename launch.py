import asyncio
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event

from viewer.console import ConsoleStop, log_stop

HOST = "127.0.0.1"
PORT = 8765


def meets_requirements(requirements):
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        package, expected = line.split("==", 1)
        try:
            if version(package) != expected:
                return False
        except PackageNotFoundError:
            return False
    return True


async def serve(server, requested, listener):
    run = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not run.done():
            if requested.is_set():
                server.should_exit = True
                break
            await asyncio.wait({run}, timeout=0.05)
        await run
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


def main():
    requested = Event()
    reason = "server_exit"

    def request_stop(source):
        nonlocal reason
        reason = source
        requested.set()

    def close_timeout():
        log_stop("console_close_timeout", reason=reason, zero="unknown")

    with ConsoleStop(request_stop, close_timeout, title="EtherCAT Viewer 后端"):
        requirements = Path(__file__).resolve().parent / "requirements.txt"
        if not meets_requirements(requirements):
            subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:",
                            "-r", str(requirements)], check=True)
        if requested.is_set():
            return 0

        import uvicorn

        # Reserve the port before starting the lifespan or spawning a bus worker.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                listener.bind((HOST, PORT))
            except OSError as exc:
                print(f"无法监听 {HOST}:{PORT}。请检查端口是否已被占用。{exc}", file=sys.stderr)
                return 1

            print(f"EtherCAT Viewer 后端 · http://{HOST}:{PORT}/", flush=True)
            print("停止服务：按 Ctrl+C 或点击此窗口右上角 ×。", flush=True)
            server = uvicorn.Server(uvicorn.Config("viewer.server:app", host=HOST, port=PORT,
                                                   timeout_graceful_shutdown=1))
            try:
                asyncio.run(serve(server, requested, listener))
            except KeyboardInterrupt:
                reason = "ctrl_c"
            log_stop("service_stop", reason=reason, started=server.started)
            return 0 if server.started or requested.is_set() else 1


if __name__ == "__main__":
    sys.exit(main())
