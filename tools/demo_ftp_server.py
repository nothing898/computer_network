"""Small local FTP server for testing src/ftp_client.py.

This is not a production FTP server. It implements only the commands needed by
the course client: USER, PASS, TYPE, PWD, CWD, CDUP, EPSV, PASV, LIST, SIZE,
REST, RETR, STOR, APPE and QUIT.
"""

from __future__ import annotations

import argparse
import os
import socket
from pathlib import Path


BUFFER_SIZE = 64 * 1024


class DemoFTPServer:
    def __init__(self, host: str, port: int, root: Path) -> None:
        self.host = host
        self.port = port
        self.root = root.resolve()
        self.cwd = Path("/")
        self.restart_offset = 0
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    def serve_forever(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(5)
        actual_host, actual_port = self.server_socket.getsockname()
        print(f"Demo FTP server listening on {actual_host}:{actual_port}")
        print(f"Root directory: {self.root}")
        print("Login with any username and password. Press Ctrl+C to stop.")

        try:
            while True:
                conn, addr = self.server_socket.accept()
                print(f"Client connected: {addr[0]}:{addr[1]}")
                self.handle_client(conn)
        finally:
            self.server_socket.close()

    def handle_client(self, conn: socket.socket) -> None:
        data_listener: socket.socket | None = None
        self.cwd = Path("/")
        self.restart_offset = 0

        with conn, conn.makefile("r", encoding="latin-1", newline="\r\n") as reader:
            self.send(conn, "220 Demo FTP server ready")
            while True:
                line = reader.readline()
                if not line:
                    break
                command = line.rstrip("\r\n")
                verb, _, arg = command.partition(" ")
                verb = verb.upper()
                print(f"< {command}")

                try:
                    if verb == "USER":
                        self.send(conn, "331 Password required")
                    elif verb == "PASS":
                        self.send(conn, "230 Login successful")
                    elif verb == "TYPE":
                        self.send(conn, "200 Type set")
                    elif verb == "PWD":
                        self.send(conn, f'257 "{self.ftp_path(self.cwd)}"')
                    elif verb == "CWD":
                        self.change_dir(arg)
                        self.send(conn, "250 Directory changed")
                    elif verb == "CDUP":
                        self.cwd = self.cwd.parent if str(self.cwd) != "/" else Path("/")
                        self.send(conn, "250 Directory changed")
                    elif verb == "EPSV":
                        data_listener = self.open_data_listener()
                        data_port = data_listener.getsockname()[1]
                        self.send(conn, f"229 Entering Extended Passive Mode (|||{data_port}|)")
                    elif verb == "PASV":
                        data_listener = self.open_data_listener()
                        data_port = data_listener.getsockname()[1]
                        nums = self.host.split(".") if self.host != "0.0.0.0" else ["127", "0", "0", "1"]
                        self.send(
                            conn,
                            "227 Entering Passive Mode "
                            f"({','.join(nums)},{data_port // 256},{data_port % 256})",
                        )
                    elif verb == "LIST":
                        self.require_data_listener(data_listener)
                        self.send(conn, "150 Opening data connection")
                        self.send_listing(data_listener)
                        data_listener = None
                        self.send(conn, "226 Transfer complete")
                    elif verb == "SIZE":
                        path = self.resolve_path(arg)
                        if path.is_file():
                            self.send(conn, f"213 {path.stat().st_size}")
                        else:
                            self.send(conn, "550 File not found")
                    elif verb == "REST":
                        self.restart_offset = int(arg)
                        self.send(conn, "350 Restart position accepted")
                    elif verb == "RETR":
                        self.require_data_listener(data_listener)
                        self.retrieve(conn, data_listener, arg)
                        data_listener = None
                    elif verb == "STOR":
                        self.require_data_listener(data_listener)
                        self.store(conn, data_listener, arg, append=False)
                        data_listener = None
                    elif verb == "APPE":
                        self.require_data_listener(data_listener)
                        self.store(conn, data_listener, arg, append=True)
                        data_listener = None
                    elif verb == "QUIT":
                        self.send(conn, "221 Goodbye")
                        break
                    else:
                        self.send(conn, "502 Command not implemented")
                except Exception as exc:
                    self.send(conn, f"550 {exc}")

    def open_data_listener(self) -> socket.socket:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, 0))
        listener.listen(1)
        return listener

    @staticmethod
    def require_data_listener(listener: socket.socket | None) -> None:
        if listener is None:
            raise RuntimeError("Use PASV or EPSV before data transfer")

    def send_listing(self, listener: socket.socket) -> None:
        current = self.resolve_path(".")
        rows = []
        for child in sorted(current.iterdir(), key=lambda item: (item.is_file(), item.name.lower())):
            kind = "d" if child.is_dir() else "-"
            size = child.stat().st_size if child.is_file() else 0
            rows.append(f"{kind}rw-r--r-- 1 user group {size:>8} Jan 01 00:00 {child.name}\r\n")

        with listener:
            data_conn, _ = listener.accept()
            with data_conn:
                data_conn.sendall("".join(rows).encode("utf-8"))

    def retrieve(self, conn: socket.socket, listener: socket.socket, name: str) -> None:
        path = self.resolve_path(name)
        if not path.is_file():
            self.send(conn, "550 File not found")
            return

        self.send(conn, "150 Opening data connection")
        with listener:
            data_conn, _ = listener.accept()
            with data_conn, path.open("rb") as source:
                if self.restart_offset:
                    source.seek(self.restart_offset)
                while True:
                    chunk = source.read(BUFFER_SIZE)
                    if not chunk:
                        break
                    data_conn.sendall(chunk)
        self.restart_offset = 0
        self.send(conn, "226 Transfer complete")

    def store(self, conn: socket.socket, listener: socket.socket, name: str, append: bool) -> None:
        path = self.resolve_path(name)
        if not self.is_safe_path(path):
            self.send(conn, "550 Invalid path")
            return
        path.parent.mkdir(parents=True, exist_ok=True)

        self.send(conn, "150 Opening data connection")
        mode = "ab" if append else "wb"
        with listener:
            data_conn, _ = listener.accept()
            with data_conn, path.open(mode) as target:
                while True:
                    chunk = data_conn.recv(BUFFER_SIZE)
                    if not chunk:
                        break
                    target.write(chunk)
        self.send(conn, "226 Transfer complete")

    def change_dir(self, name: str) -> None:
        path = self.resolve_path(name)
        if not path.is_dir():
            raise RuntimeError("Directory not found")
        relative = path.relative_to(self.root)
        self.cwd = Path("/") / relative

    def resolve_path(self, name: str) -> Path:
        if not name or name == ".":
            candidate = self.root / str(self.cwd).lstrip("/")
        elif name.startswith("/"):
            candidate = self.root / name.lstrip("/")
        else:
            candidate = self.root / str(self.cwd).lstrip("/") / name
        candidate = candidate.resolve()
        if not self.is_safe_path(candidate):
            raise RuntimeError("Path escapes server root")
        return candidate

    def is_safe_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    @staticmethod
    def ftp_path(path: Path) -> str:
        value = "/" if str(path) == "." else str(path).replace(os.sep, "/")
        return value if value.startswith("/") else f"/{value}"

    @staticmethod
    def send(conn: socket.socket, line: str) -> None:
        print(f"> {line}")
        conn.sendall((line + "\r\n").encode("latin-1"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local demo FTP server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2121)
    parser.add_argument("--root", type=Path, default=Path("demo_ftp_root"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    DemoFTPServer(args.host, args.port, args.root).serve_forever()


if __name__ == "__main__":
    main()
