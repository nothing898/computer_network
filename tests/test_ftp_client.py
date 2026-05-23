from __future__ import annotations

import socket
import tempfile
import threading
import unittest
from pathlib import Path

from src.ftp_client import RawFTPClient, parse_list_line


class MiniFTPServer:
    def __init__(self) -> None:
        self.files = {"remote.txt": b"hello world"}
        self.rest_offset = 0
        self.cwd = "/"
        self.ready = threading.Event()
        self.done = threading.Event()
        self.control = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.control.bind(("127.0.0.1", 0))
        self.control.listen(1)
        self.port = self.control.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> None:
        self.thread.start()
        self.ready.wait(timeout=2)

    def stop(self) -> None:
        self.done.set()
        try:
            socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
        except OSError:
            pass
        self.thread.join(timeout=2)
        self.control.close()

    def _serve(self) -> None:
        self.ready.set()
        try:
            conn, _addr = self.control.accept()
        except OSError:
            return
        with conn, conn.makefile("r", encoding="latin-1", newline="\r\n") as reader:
            self._send(conn, "220 mini ftp ready")
            data_listener = None
            while not self.done.is_set():
                line = reader.readline()
                if not line:
                    break
                command = line.rstrip("\r\n")
                verb, _, arg = command.partition(" ")
                verb = verb.upper()

                if verb == "USER":
                    self._send(conn, "331 password required")
                elif verb == "PASS":
                    self._send(conn, "230 logged in")
                elif verb == "TYPE":
                    self._send(conn, "200 type set")
                elif verb == "PWD":
                    self._send(conn, f'257 "{self.cwd}"')
                elif verb == "SIZE":
                    payload = self.files.get(arg)
                    self._send(conn, f"213 {len(payload)}" if payload is not None else "550 missing")
                elif verb == "REST":
                    self.rest_offset = int(arg)
                    self._send(conn, "350 restart accepted")
                elif verb == "EPSV":
                    self._send(conn, "502 epsv unavailable")
                elif verb == "PASV":
                    data_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    data_listener.bind(("127.0.0.1", 0))
                    data_listener.listen(1)
                    port = data_listener.getsockname()[1]
                    self._send(conn, f"227 Entering Passive Mode (127,0,0,1,{port // 256},{port % 256})")
                elif verb == "LIST":
                    self._send(conn, "150 opening list")
                    assert data_listener is not None
                    with data_listener:
                        data_conn, _ = data_listener.accept()
                        with data_conn:
                            data_conn.sendall(b"-rw-r--r-- 1 user group 11 Jan 01 12:00 remote.txt\r\n")
                    self._send(conn, "226 list done")
                elif verb == "RETR":
                    self._send(conn, "150 opening data")
                    assert data_listener is not None
                    with data_listener:
                        data_conn, _ = data_listener.accept()
                        with data_conn:
                            data_conn.sendall(self.files[arg][self.rest_offset :])
                    self.rest_offset = 0
                    self._send(conn, "226 transfer done")
                elif verb in {"STOR", "APPE"}:
                    self._send(conn, "150 opening data")
                    assert data_listener is not None
                    chunks = []
                    with data_listener:
                        data_conn, _ = data_listener.accept()
                        with data_conn:
                            while True:
                                chunk = data_conn.recv(8192)
                                if not chunk:
                                    break
                                chunks.append(chunk)
                    incoming = b"".join(chunks)
                    self.files[arg] = self.files.get(arg, b"") + incoming if verb == "APPE" else incoming
                    self._send(conn, "226 transfer done")
                elif verb == "QUIT":
                    self._send(conn, "221 bye")
                    break
                else:
                    self._send(conn, "200 ok")

    @staticmethod
    def _send(conn: socket.socket, line: str) -> None:
        conn.sendall((line + "\r\n").encode("latin-1"))


class FTPClientTests(unittest.TestCase):
    def test_list_download_resume_and_upload_resume(self) -> None:
        server = MiniFTPServer()
        server.start()
        client = RawFTPClient(timeout=2)
        with tempfile.TemporaryDirectory() as tmp:
            try:
                client.connect("127.0.0.1", server.port)
                client.login("user", "pass")
                self.assertEqual(client.list()[0].name, "remote.txt")

                target = Path(tmp) / "remote.txt"
                target.write_bytes(b"hello ")
                client.download("remote.txt", target, resume=True)
                self.assertEqual(target.read_bytes(), b"hello world")

                upload = Path(tmp) / "upload.txt"
                upload.write_bytes(b"abcdef")
                server.files["upload.txt"] = b"abc"
                client.upload(upload, "upload.txt", resume=True)
                self.assertEqual(server.files["upload.txt"], b"abcdef")
            finally:
                client.close()
                server.stop()

    def test_parse_dos_list_line(self) -> None:
        entry = parse_list_line("01-01-26  12:00PM       <DIR>          docs")
        self.assertEqual(entry.name, "docs")
        self.assertEqual(entry.kind, "dir")


if __name__ == "__main__":
    unittest.main()
