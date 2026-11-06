"""Dotenv cannot opt fixture tests into live mode, including in a fresh process."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from app.adapters.llm import resolve_mode
from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient

LIVE_DOTENV = """\
OOS_LIVE_ENABLED=true
OOS_LLM_PROVIDER=openai
OOS_LLM_MODEL=gpt-6-luna
OOS_PRICE_IN_PER_MTOK=0.10
OOS_PRICE_OUT_PER_MTOK=0.50
OOS_LLM_API_KEY=fake-env-isolation-key
"""

# Loaded with -p before conftest/collection; block DNS as well as direct sockets.
SOCKET_GUARD = """\
import ipaddress
import os
import socket
import threading
from pathlib import Path

count_file = Path(os.environ["OUTBOUND_COUNT_FILE"])
count_file.write_text("0")
count = 0
lock = threading.Lock()
original_getaddrinfo = socket.getaddrinfo
original_connect = socket.socket.connect
original_connect_ex = socket.socket.connect_ex

def is_loopback(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or bool(
        getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback
    )

def block():
    global count
    with lock:
        count += 1
        count_file.write_text(str(count))
    raise OSError("Non-loopback network access blocked by pytest socket guard")

def getaddrinfo(host, *args, **kwargs):
    if not is_loopback(host):
        block()
    return original_getaddrinfo(host, *args, **kwargs)

def connect(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback(address[0]):
        block()
    return original_connect(sock, address)

def connect_ex(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not is_loopback(address[0]):
        block()
    return original_connect_ex(sock, address)

socket.getaddrinfo = getaddrinfo
socket.socket.connect = connect
socket.socket.connect_ex = connect_ex
"""


def test_default_settings_ignore_live_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(LIVE_DOTENV)
    monkeypatch.chdir(tmp_path)

    assert Settings().live_enabled is False
    assert resolve_mode(get_settings()).mode == "FIXTURE"
    assert get_settings().llm_api_key_value() is None
    with TestClient(base_url="http://127.0.0.1", app=create_app()) as client:
        assert client.get("/health").json()["mode"] == "FIXTURE"


def test_subprocess_dispatch_ignores_live_dotenv_without_outbound_sockets(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(LIVE_DOTENV)
    (tmp_path / "outbound_guard.py").write_text(textwrap.dedent(SOCKET_GUARD))
    count_file = tmp_path / "outbound_count.txt"
    env = {
        key: value for key, value in os.environ.items() if not key.startswith(("OOS_", "PYTEST_"))
    }
    env.update(
        PYTHONPATH=os.pathsep.join([str(tmp_path), str(repo)]),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        OUTBOUND_COUNT_FILE=str(count_file),
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(repo / "pyproject.toml"),
            "--rootdir",
            str(repo),
            "-o",
            f"cache_dir={tmp_path / 'pytest_cache'}",
            "-p",
            "outbound_guard",
            str(repo / "tests/test_stage3.py"),
            str(repo / "tests/test_stage1c.py"),
            f"{__file__}::test_default_settings_ignore_live_dotenv",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = result.stdout + result.stderr
    assert count_file.is_file(), output
    assert int(count_file.read_text()) == 0, output
    assert result.returncode == 0, output
