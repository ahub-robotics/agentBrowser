"""Offline checks for explicit, portable environment loading."""

import importlib.util
import os
from unittest.mock import Mock

import pytest

from agentrun import config


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "environ", {})


def test_missing_default_environment_is_optional():
    assert config.load_environment() == ()


def test_local_dotenv_supports_quotes_comments_and_interpolation(tmp_path, monkeypatch):
    monkeypatch.setenv("ORIGIN", "process")
    local = tmp_path / ".env"
    local.write_text('export VALUE="hello world" # comment\nURL="${ORIGIN}/path"\n', encoding="utf-8")
    assert config.load_environment() == (local,)
    assert os.environ["VALUE"] == "hello world"
    assert os.environ["URL"] == "process/path"


def test_pointer_loads_external_file_and_preserves_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIORITY", "process")
    local = tmp_path / ".env"
    referenced = tmp_path / "secrets.env"
    local.write_text("AGENTRUN_ENV_FILE=secrets.env\nPRIORITY=local\nLOCAL=local\n", encoding="utf-8")
    referenced.write_text("PRIORITY=external\nLOCAL=external\nEXTERNAL=available\n", encoding="utf-8")
    assert config.load_environment() == (local, referenced)
    assert os.environ["PRIORITY"] == "process"
    assert os.environ["LOCAL"] == "local"
    assert os.environ["EXTERNAL"] == "available"


def test_process_pointer_overrides_local_pointer(tmp_path, monkeypatch):
    referenced = tmp_path / "process.env"
    referenced.write_text("VALUE=process-file\n", encoding="utf-8")
    (tmp_path / ".env").write_text("AGENTRUN_ENV_FILE=missing.env\n", encoding="utf-8")
    monkeypatch.setenv("AGENTRUN_ENV_FILE", str(referenced))
    config.load_environment()
    assert os.environ["VALUE"] == "process-file"


def test_explicit_file_overrides_default_file_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRUN_ENV_FILE", "missing.env")
    monkeypatch.setenv("PRESERVED", "process")
    (tmp_path / ".env").write_text("DEFAULT=not-loaded\n", encoding="utf-8")
    explicit = tmp_path / "explicit.env"
    explicit.write_text("VALUE=explicit\nPRESERVED=file\n", encoding="utf-8")
    assert config.load_environment(explicit) == (explicit,)
    assert os.environ["VALUE"] == "explicit"
    assert os.environ["PRESERVED"] == "process"
    assert "DEFAULT" not in os.environ


@pytest.mark.parametrize("source", ["argument", "pointer"])
def test_explicit_missing_environment_is_an_error(tmp_path, monkeypatch, source):
    missing = tmp_path / "missing.env"
    if source == "pointer":
        monkeypatch.setenv("AGENTRUN_ENV_FILE", str(missing))
    with pytest.raises(config.EnvironmentFileError, match="does not exist"):
        config.load_environment(missing if source == "argument" else None)


def test_environment_path_must_be_a_file(tmp_path):
    with pytest.raises(config.EnvironmentFileError, match="regular file"):
        config.load_environment(tmp_path)


def test_invalid_encoding_never_echoes_file_contents(tmp_path):
    secret = tmp_path / "secret.env"
    secret.write_bytes(b"KEY=do-not-expose\xff")
    with pytest.raises(config.EnvironmentFileError) as error:
        config.load_environment(secret)
    assert "do-not-expose" not in str(error.value)


def test_self_reference_is_not_loaded_twice(tmp_path, monkeypatch):
    local = tmp_path / ".env"
    local.write_text("AGENTRUN_ENV_FILE=.env\n", encoding="utf-8")
    loader = Mock(wraps=config.load_dotenv)
    monkeypatch.setattr(config, "load_dotenv", loader)
    assert config.load_environment() == (local,)
    assert loader.call_count == 1


def test_import_does_not_load_environment(tmp_path):
    (tmp_path / ".env").write_text("IMPORT_SIDE_EFFECT=forbidden\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location("isolated_agentrun_config", config.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert "IMPORT_SIDE_EFFECT" not in os.environ
