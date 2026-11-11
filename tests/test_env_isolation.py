"""Dotenv cannot opt fixture tests into live mode, including in a fresh process."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from app import cli
from app.adapters.llm import resolve_mode
from app.config import Settings, get_settings
from app.main import create_app
from fastapi.testclient import TestClient
from pydantic import SecretStr
from scripts.live_smoke import SECRET_PATTERN

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


def test_auth_preflight_ignores_dotenv_and_never_opens_http_or_database(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / ".env").write_text(LIVE_DOTENV)
    monkeypatch.chdir(tmp_path)

    def forbidden(*args, **kwargs):
        pytest.fail("Local auth preflight must not access HTTP, a ledger, or secret plaintext")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr(cli, "make_engine", forbidden)
    monkeypatch.setattr(SecretStr, "get_secret_value", forbidden)
    assert cli.main(["check-model-auth"]) == 0
    captured = capsys.readouterr()
    assert "OOS_LIVE_ENABLED：无" in captured.out
    assert "OOS_LLM_API_KEY：无" in captured.out
    assert "fake-env-isolation-key" not in captured.out + captured.err
    assert not SECRET_PATTERN.search(captured.out + captured.err)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("probe", [False, True])
def test_auth_standard_openai_key_only_gets_mapping_hint(monkeypatch, capsys, probe):
    fake_key = "sk-standard_only_offline_123456"
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)
    monkeypatch.setenv("OOS_LIVE_ENABLED", "true")
    monkeypatch.setenv("OOS_LLM_PROVIDER", "openai")
    monkeypatch.setenv("OOS_LLM_MODEL", "offline-auth-model")
    monkeypatch.setenv("OOS_PRICE_IN_PER_MTOK", "0.10")
    monkeypatch.setenv("OOS_PRICE_OUT_PER_MTOK", "0.50")

    def forbidden(*args, **kwargs):
        pytest.fail("Standard key must never authorize a model request")

    monkeypatch.setattr("httpx.Client", forbidden)
    monkeypatch.setattr(cli, "make_engine", forbidden)
    monkeypatch.setattr(SecretStr, "get_secret_value", forbidden)
    assert cli.main(["check-model-auth", *(["--probe"] if probe else [])]) == (2 if probe else 0)
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "OOS_LLM_API_KEY：无" in output
    assert "项目只读 OOS_LLM_API_KEY，请在运行时把同一密钥映射到该变量名" in output
    assert fake_key not in output and not SECRET_PATTERN.search(output)
    if probe:
        assert '"status": "KEY_NOT_CONFIGURED"' in captured.out


@pytest.mark.parametrize("probe", [False, True])
def test_auth_settings_errors_never_print_input_or_traceback(monkeypatch, capsys, probe):
    fake_key = "sk-invalid_settings_offline_123456"
    monkeypatch.setenv("OOS_LIVE_ENABLED", fake_key)
    assert cli.main(["check-model-auth", *(["--probe"] if probe else [])]) == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "AUTH_CONFIGURATION" in output
    for forbidden in (fake_key, "Traceback", "ValidationError", "input_value"):
        assert forbidden not in output
    assert not SECRET_PATTERN.search(output)


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
