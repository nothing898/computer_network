"""FTP client with a Tkinter GUI and socket-level protocol implementation.

The assignment requires the FTP protocol to be implemented from the socket
layer upward. This module intentionally avoids ftplib and talks to the FTP
server through raw TCP sockets for the control and passive data channels.
"""

from __future__ import annotations

import queue
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


BUFFER_SIZE = 64 * 1024


class FTPError(RuntimeError):
    """Raised when the FTP server returns an unexpected response."""


@dataclass
class FTPResponse:
    code: int
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class RemoteEntry:
    name: str
    kind: str
    size: str
    raw: str


def parse_list_line(line: str) -> RemoteEntry:
    """Parse a common UNIX or Windows/DOS FTP LIST row."""
    stripped = line.strip()
    if not stripped:
        return RemoteEntry("", "file", "", line)

    # UNIX style: drwxr-xr-x  2 user group 4096 Jan 01 12:00 folder
    unix_match = re.match(
        r"^(?P<mode>[dl-][^\s]*)\s+\S+\s+\S+\s+\S+\s+"
        r"(?P<size>\d+)\s+\w+\s+\d+\s+[\d:]+\s+(?P<name>.+)$",
        stripped,
    )
    if unix_match:
        mode = unix_match.group("mode")
        return RemoteEntry(
            name=unix_match.group("name"),
            kind="dir" if mode.startswith("d") else "file",
            size=unix_match.group("size") if not mode.startswith("d") else "",
            raw=line,
        )

    # Windows style: 01-01-26  12:00PM       <DIR>          folder
    win_match = re.match(
        r"^\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}[AP]M\s+"
        r"(?P<size_or_dir><DIR>|\d+)\s+(?P<name>.+)$",
        stripped,
        re.IGNORECASE,
    )
    if win_match:
        size_or_dir = win_match.group("size_or_dir")
        is_dir = size_or_dir.upper() == "<DIR>"
        return RemoteEntry(
            name=win_match.group("name"),
            kind="dir" if is_dir else "file",
            size="" if is_dir else size_or_dir,
            raw=line,
        )

    return RemoteEntry(stripped.split()[-1], "file", "", line)


class RawFTPClient:
    """Small FTP client implemented directly on top of sockets."""

    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout
        self.control_socket: socket.socket | None = None
        self.control_file = None

    @property
    def connected(self) -> bool:
        return self.control_socket is not None

    def connect(self, host: str, port: int = 21) -> FTPResponse:
        self.close()
        control = socket.create_connection((host, port), timeout=self.timeout)
        control.settimeout(self.timeout)
        self.control_socket = control
        self.control_file = control.makefile("r", encoding="latin-1", newline="\r\n")
        response = self._read_response()
        self._expect(response, {220})
        return response

    def login(self, username: str, password: str) -> None:
        response = self.command(f"USER {username}", expected={230, 331})
        if response.code == 331:
            self.command(f"PASS {password}", expected={230, 202})
        self.command("TYPE I", expected={200})

    def close(self) -> None:
        if self.control_socket is not None:
            try:
                self.command("QUIT", expected={221, 226, 250}, allow_error=True)
            except Exception:
                pass
        if self.control_file is not None:
            try:
                self.control_file.close()
            except Exception:
                pass
        if self.control_socket is not None:
            try:
                self.control_socket.close()
            except Exception:
                pass
        self.control_socket = None
        self.control_file = None

    def command(
        self,
        command: str,
        expected: set[int] | None = None,
        allow_error: bool = False,
    ) -> FTPResponse:
        if self.control_socket is None:
            raise FTPError("Not connected.")
        data = f"{command}\r\n".encode("latin-1")
        self.control_socket.sendall(data)
        response = self._read_response()
        if expected is not None and not allow_error:
            self._expect(response, expected)
        return response

    def pwd(self) -> str:
        response = self.command("PWD", expected={257})
        match = re.search(r'"([^"]*)"', response.text)
        return match.group(1) if match else response.text

    def cwd(self, path: str) -> None:
        self.command(f"CWD {path}", expected={250})

    def cdup(self) -> None:
        self.command("CDUP", expected={200, 250})

    def list(self) -> list[RemoteEntry]:
        payload = self._transfer_text("LIST")
        entries = []
        for line in payload.splitlines():
            entry = parse_list_line(line)
            if entry.name and entry.name not in {".", ".."}:
                entries.append(entry)
        return entries

    def size(self, remote_path: str) -> int | None:
        response = self.command(f"SIZE {remote_path}", expected=None, allow_error=True)
        if response.code == 213:
            try:
                return int(response.text.split()[-1])
            except (ValueError, IndexError):
                return None
        return None

    def download(
        self,
        remote_name: str,
        local_path: Path,
        resume: bool = True,
        progress: Callable[[int, int | None], None] | None = None,
    ) -> None:
        offset = local_path.stat().st_size if resume and local_path.exists() else 0
        total = self.size(remote_name)
        if offset and total is not None and offset >= total:
            if progress:
                progress(total, total)
            return

        data_socket = self._open_passive_data_socket()
        if offset:
            self.command(f"REST {offset}", expected={350})
        self.command(f"RETR {remote_name}", expected={125, 150})

        mode = "ab" if offset else "wb"
        transferred = offset
        with data_socket, local_path.open(mode) as output:
            while True:
                chunk = data_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                transferred += len(chunk)
                if progress:
                    progress(transferred, total)

        self._expect(self._read_response(), {226, 250})

    def upload(
        self,
        local_path: Path,
        remote_name: str,
        resume: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        total = local_path.stat().st_size
        remote_size = self.size(remote_name) if resume else None
        offset = remote_size or 0
        if offset > total:
            offset = 0
        if offset == total and total > 0:
            if progress:
                progress(total, total)
            return

        data_socket = self._open_passive_data_socket()
        command = f"APPE {remote_name}" if offset else f"STOR {remote_name}"
        self.command(command, expected={125, 150})

        sent = offset
        with data_socket, local_path.open("rb") as source:
            if offset:
                source.seek(offset)
            while True:
                chunk = source.read(BUFFER_SIZE)
                if not chunk:
                    break
                data_socket.sendall(chunk)
                sent += len(chunk)
                if progress:
                    progress(sent, total)

        self._expect(self._read_response(), {226, 250})

    def _transfer_text(self, command: str) -> str:
        data_socket = self._open_passive_data_socket()
        self.command(command, expected={125, 150})
        chunks: list[bytes] = []
        with data_socket:
            while True:
                chunk = data_socket.recv(BUFFER_SIZE)
                if not chunk:
                    break
                chunks.append(chunk)
        self._expect(self._read_response(), {226, 250})
        payload = b"".join(chunks)
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            return payload.decode("latin-1", errors="replace")

    def _open_passive_data_socket(self) -> socket.socket:
        if self.control_socket is None:
            raise FTPError("Not connected.")
        response = self.command("EPSV", expected=None, allow_error=True)
        if response.code == 229:
            match = re.search(r"\(\|\|\|(\d+)\|\)", response.text)
            if not match:
                raise FTPError(f"Cannot parse EPSV response: {response.text}")
            host = self.control_socket.getpeername()[0]
            port = int(match.group(1))
            return socket.create_connection((host, port), timeout=self.timeout)

        response = self.command("PASV", expected={227})
        numbers = re.findall(r"\d+", response.text)
        if len(numbers) < 6:
            raise FTPError(f"Cannot parse PASV response: {response.text}")
        host = ".".join(numbers[-6:-2])
        port = int(numbers[-2]) * 256 + int(numbers[-1])
        return socket.create_connection((host, port), timeout=self.timeout)

    def _read_response(self) -> FTPResponse:
        if self.control_file is None:
            raise FTPError("Not connected.")

        first = self.control_file.readline()
        if not first:
            raise FTPError("Server closed the control connection.")
        first = first.rstrip("\r\n")
        if len(first) < 3 or not first[:3].isdigit():
            raise FTPError(f"Invalid FTP response: {first}")

        code = int(first[:3])
        lines = [first]
        if len(first) > 3 and first[3] == "-":
            terminator = f"{first[:3]} "
            while True:
                line = self.control_file.readline()
                if not line:
                    raise FTPError("Server closed the control connection.")
                line = line.rstrip("\r\n")
                lines.append(line)
                if line.startswith(terminator):
                    break
        return FTPResponse(code=code, lines=lines)

    @staticmethod
    def _expect(response: FTPResponse, expected: Iterable[int]) -> None:
        expected_set = set(expected)
        if response.code not in expected_set:
            raise FTPError(f"Expected {sorted(expected_set)}, got {response.code}: {response.text}")


class FTPClientApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Socket FTP Client")
        self.geometry("1060x700")
        self.minsize(920, 600)

        self.client = RawFTPClient()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.local_dir = Path.home()
        self.remote_entries: dict[str, RemoteEntry] = {}

        self._build_ui()
        self._load_local_files()
        self.after(100, self._drain_events)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        connection = ttk.Frame(self, padding=10)
        connection.grid(row=0, column=0, sticky="ew")
        for index in (1, 3, 5):
            connection.columnconfigure(index, weight=1)

        ttk.Label(connection, text="Host").grid(row=0, column=0, sticky="w")
        self.host_var = tk.StringVar(value="localhost")
        ttk.Entry(connection, textvariable=self.host_var).grid(row=0, column=1, sticky="ew", padx=(4, 10))

        ttk.Label(connection, text="Port").grid(row=0, column=2, sticky="w")
        self.port_var = tk.StringVar(value="21")
        ttk.Entry(connection, textvariable=self.port_var, width=6).grid(row=0, column=3, sticky="ew", padx=(4, 10))

        ttk.Label(connection, text="User").grid(row=0, column=4, sticky="w")
        self.user_var = tk.StringVar(value="anonymous")
        ttk.Entry(connection, textvariable=self.user_var).grid(row=0, column=5, sticky="ew", padx=(4, 10))

        ttk.Label(connection, text="Password").grid(row=0, column=6, sticky="w")
        self.password_var = tk.StringVar(value="anonymous@example.com")
        ttk.Entry(connection, textvariable=self.password_var, show="*").grid(row=0, column=7, sticky="ew", padx=(4, 10))

        ttk.Button(connection, text="Connect", command=self._connect).grid(row=0, column=8, padx=(0, 6))
        ttk.Button(connection, text="Disconnect", command=self._disconnect).grid(row=0, column=9)

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.grid(row=1, column=0, sticky="nsew", padx=10)

        local_panel = ttk.Frame(panes, padding=(0, 0, 6, 0))
        remote_panel = ttk.Frame(panes, padding=(6, 0, 0, 0))
        panes.add(local_panel, weight=1)
        panes.add(remote_panel, weight=1)

        self._build_local_panel(local_panel)
        self._build_remote_panel(remote_panel)

        bottom = ttk.Frame(self, padding=10)
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(0, weight=1)

        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bottom, textvariable=self.status_var, width=30).grid(row=0, column=1, sticky="e")

        log_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        log_frame.grid(row=3, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word")
        self.log.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=log_scrollbar.set)

    def _build_local_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        ttk.Label(parent, text="Local Files").grid(row=0, column=0, sticky="w")
        tools = ttk.Frame(parent)
        tools.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        tools.columnconfigure(0, weight=1)
        self.local_path_var = tk.StringVar()
        ttk.Entry(tools, textvariable=self.local_path_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(tools, text="Choose", command=self._choose_local_dir).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(tools, text="Refresh", command=self._load_local_files).grid(row=0, column=2)

        self.local_tree = ttk.Treeview(parent, columns=("kind", "size"), show="headings", selectmode="browse")
        self.local_tree.heading("kind", text="Type")
        self.local_tree.heading("size", text="Size")
        self.local_tree.column("kind", width=90, stretch=False)
        self.local_tree.column("size", width=110, stretch=False, anchor="e")
        self.local_tree.grid(row=2, column=0, sticky="nsew")
        self.local_tree.bind("<Double-1>", lambda _event: self._open_local_selection())

        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="Upload ->", command=self._upload_selection).pack(side=tk.RIGHT)

    def _build_remote_panel(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        ttk.Label(parent, text="Remote Files").grid(row=0, column=0, sticky="w")
        tools = ttk.Frame(parent)
        tools.grid(row=1, column=0, sticky="ew", pady=(6, 6))
        tools.columnconfigure(0, weight=1)
        self.remote_path_var = tk.StringVar(value="/")
        ttk.Entry(tools, textvariable=self.remote_path_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(tools, text="Parent", command=self._remote_parent).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(tools, text="Refresh", command=self._refresh_remote).grid(row=0, column=2)

        self.remote_tree = ttk.Treeview(parent, columns=("kind", "size"), show="headings", selectmode="browse")
        self.remote_tree.heading("kind", text="Type")
        self.remote_tree.heading("size", text="Size")
        self.remote_tree.column("kind", width=90, stretch=False)
        self.remote_tree.column("size", width=110, stretch=False, anchor="e")
        self.remote_tree.grid(row=2, column=0, sticky="nsew")
        self.remote_tree.bind("<Double-1>", lambda _event: self._open_remote_selection())

        buttons = ttk.Frame(parent)
        buttons.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="<- Download", command=self._download_selection).pack(side=tk.LEFT)

    def _connect(self) -> None:
        host = self.host_var.get().strip()
        user = self.user_var.get().strip()
        password = self.password_var.get()
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("Invalid port", "Port must be a number.")
            return
        self._run_worker(lambda: self._connect_worker(host, port, user, password), "Connecting...")

    def _connect_worker(self, host: str, port: int, user: str, password: str) -> None:
        greeting = self.client.connect(host, port)
        self._post("log", greeting.text)
        self.client.login(user, password)
        self._post("log", "Login succeeded. Binary transfer mode enabled.")
        self._refresh_remote_worker()

    def _disconnect(self) -> None:
        self.client.close()
        self.remote_entries.clear()
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.status_var.set("Disconnected")
        self._append_log("Disconnected.")

    def _choose_local_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.local_dir)
        if chosen:
            self.local_dir = Path(chosen)
            self._load_local_files()

    def _load_local_files(self) -> None:
        self.local_path_var.set(str(self.local_dir))
        self.local_tree.delete(*self.local_tree.get_children())
        if self.local_dir.parent != self.local_dir:
            self.local_tree.insert("", tk.END, iid="..", text="..", values=("dir", ""))
        try:
            children = sorted(self.local_dir.iterdir(), key=lambda path: (path.is_file(), path.name.lower()))
            for child in children:
                kind = "dir" if child.is_dir() else "file"
                size = "" if child.is_dir() else str(child.stat().st_size)
                self.local_tree.insert("", tk.END, iid=str(child), values=(kind, size), text=child.name)
                self.local_tree.set(str(child), "kind", kind)
                self.local_tree.set(str(child), "size", size)
                self.local_tree.item(str(child), values=(kind, size))
                self.local_tree.heading("#0", text="Name")
        except OSError as exc:
            messagebox.showerror("Local files", str(exc))

        self.local_tree.configure(show="tree headings")
        self.local_tree.heading("#0", text="Name")
        self.local_tree.column("#0", width=260, stretch=True)

    def _open_local_selection(self) -> None:
        selection = self.local_tree.selection()
        if not selection:
            return
        item = selection[0]
        if item == "..":
            self.local_dir = self.local_dir.parent
        else:
            path = Path(item)
            if path.is_dir():
                self.local_dir = path
        self._load_local_files()

    def _refresh_remote(self) -> None:
        self._run_worker(self._refresh_remote_worker, "Refreshing remote files...")

    def _refresh_remote_worker(self) -> None:
        entries = self.client.list()
        pwd = self.client.pwd()
        self._post("remote", (pwd, entries))
        self._post("log", f"Remote directory refreshed: {pwd}")

    def _remote_parent(self) -> None:
        self._run_worker(self._remote_parent_worker, "Changing remote directory...")

    def _remote_parent_worker(self) -> None:
        self.client.cdup()
        self._refresh_remote_worker()

    def _open_remote_selection(self) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            return
        entry = self.remote_entries.get(selection[0])
        if entry and entry.kind == "dir":
            self._run_worker(lambda: self._remote_cwd_worker(entry.name), "Changing remote directory...")

    def _remote_cwd_worker(self, dirname: str) -> None:
        self.client.cwd(dirname)
        self._refresh_remote_worker()

    def _upload_selection(self) -> None:
        selection = self.local_tree.selection()
        if not selection:
            messagebox.showinfo("Upload", "Choose a local file first.")
            return
        item = selection[0]
        if item == "..":
            return
        path = Path(item)
        if not path.is_file():
            messagebox.showinfo("Upload", "Only files can be uploaded.")
            return
        self._run_worker(lambda: self._upload_worker(path), f"Uploading {path.name}...")

    def _upload_worker(self, path: Path) -> None:
        self.client.upload(path, path.name, resume=True, progress=self._progress_callback)
        self._post("log", f"Upload completed: {path.name}")
        self._refresh_remote_worker()

    def _download_selection(self) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            messagebox.showinfo("Download", "Choose a remote file first.")
            return
        entry = self.remote_entries.get(selection[0])
        if not entry or entry.kind == "dir":
            messagebox.showinfo("Download", "Only files can be downloaded.")
            return
        target = self.local_dir / entry.name
        self._run_worker(lambda: self._download_worker(entry.name, target), f"Downloading {entry.name}...")

    def _download_worker(self, remote_name: str, target: Path) -> None:
        self.client.download(remote_name, target, resume=True, progress=self._progress_callback)
        self._post("log", f"Download completed: {remote_name} -> {target}")
        self._post("local_refresh", None)

    def _progress_callback(self, current: int, total: int | None) -> None:
        self._post("progress", (current, total))

    def _run_worker(self, target: Callable[[], None], status: str) -> None:
        self.status_var.set(status)
        self.progress.configure(value=0, maximum=100)

        def wrapped() -> None:
            try:
                target()
                self._post("status", "Ready")
            except Exception as exc:
                self._post("error", exc)

        threading.Thread(target=wrapped, daemon=True).start()

    def _post(self, event: str, payload: object) -> None:
        self.events.put((event, payload))

    def _drain_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._append_log(str(payload))
            elif event == "error":
                self.status_var.set("Error")
                self._append_log(f"ERROR: {payload}")
                messagebox.showerror("FTP error", str(payload))
            elif event == "status":
                self.status_var.set(str(payload))
            elif event == "progress":
                current, total = payload  # type: ignore[misc]
                if total:
                    self.progress.configure(maximum=total, value=current)
                    self.status_var.set(f"{current}/{total} bytes")
                else:
                    self.status_var.set(f"{current} bytes")
            elif event == "remote":
                pwd, entries = payload  # type: ignore[misc]
                self._load_remote_entries(str(pwd), entries)
            elif event == "local_refresh":
                self._load_local_files()
        self.after(100, self._drain_events)

    def _load_remote_entries(self, pwd: str, entries: list[RemoteEntry]) -> None:
        self.remote_path_var.set(pwd)
        self.remote_entries.clear()
        self.remote_tree.delete(*self.remote_tree.get_children())
        self.remote_tree.configure(show="tree headings")
        self.remote_tree.heading("#0", text="Name")
        self.remote_tree.column("#0", width=260, stretch=True)
        for index, entry in enumerate(entries):
            item_id = f"remote-{index}"
            self.remote_entries[item_id] = entry
            self.remote_tree.insert(
                "",
                tk.END,
                iid=item_id,
                text=entry.name,
                values=(entry.kind, entry.size),
            )

    def _append_log(self, message: str) -> None:
        self.log.insert(tk.END, message + "\n")
        self.log.see(tk.END)


def main() -> None:
    app = FTPClientApp()
    app.mainloop()


if __name__ == "__main__":
    main()
