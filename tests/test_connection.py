"""Unit tests for config loading and stdout hygiene."""

import pytest

from bigfix_root_mcp import connection


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("BES_ROOT_SERVER", "BES_USER_NAME", "BES_PASSWORD", "BES_SSL_VERIFY"):
        monkeypatch.delenv(var, raising=False)


def write_conf(path, body):
    path.write_text(body)
    return str(path)


def test_env_vars_used(monkeypatch):
    monkeypatch.setenv("BES_ROOT_SERVER", "https://bes.example.com:52311")
    monkeypatch.setenv("BES_USER_NAME", "envuser")
    monkeypatch.setenv("BES_PASSWORD", "envpass")
    config = connection.load_config()
    assert config.rootserver == "https://bes.example.com:52311"
    assert config.username == "envuser"
    assert config.password == "envpass"
    assert config.verify is False  # besapi default


def test_env_wins_over_config_file(monkeypatch, tmp_path):
    conf = write_conf(
        tmp_path / "besapi.conf",
        "[besapi]\nBES_ROOT_SERVER = https://file.example.com:52311\n"
        "BES_USER_NAME = fileuser\nBES_PASSWORD = filepass\n",
    )
    monkeypatch.setattr(connection, "CONFIG_PATHS", [conf])
    monkeypatch.setenv("BES_ROOT_SERVER", "https://env.example.com:52311")
    monkeypatch.setenv("BES_USER_NAME", "envuser")
    monkeypatch.setenv("BES_PASSWORD", "envpass")
    config = connection.load_config()
    assert config.rootserver == "https://env.example.com:52311"
    assert config.username == "envuser"


def test_config_file_used_when_no_env(monkeypatch, tmp_path):
    conf = write_conf(
        tmp_path / "besapi.conf",
        "[besapi]\nBES_ROOT_SERVER = https://file.example.com:52311\n"
        "BES_USER_NAME = fileuser\nBES_PASSWORD = filepass\n"
        "BES_SSL_VERIFY = true\n",
    )
    monkeypatch.setattr(connection, "CONFIG_PATHS", [conf])
    config = connection.load_config()
    assert config.rootserver == "https://file.example.com:52311"
    assert config.username == "fileuser"
    assert config.password == "filepass"
    assert config.verify is True


def test_missing_config_error_lists_paths_not_password(monkeypatch, tmp_path):
    monkeypatch.setattr(connection, "CONFIG_PATHS", [str(tmp_path / "nope.conf")])
    with pytest.raises(connection.ConnectionConfigError) as excinfo:
        connection.load_config()
    message = str(excinfo.value)
    assert "BES_ROOT_SERVER" in message
    assert "nope.conf" in message
    assert "password" not in message.lower() or "BES_PASSWORD" in message


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", False),
        ("false", False),
        ("False", False),
        ("0", False),
        ("no", False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("/etc/ssl/ca-bundle.pem", "/etc/ssl/ca-bundle.pem"),
        # session relevance's trust-on-first-use; besapi itself gets its default
        ("tofu", False),
        ("TOFU", False),
    ],
)
def test_ssl_verify_parsing(raw, expected):
    assert connection._parse_verify(raw) == expected


@pytest.mark.parametrize(
    ("raw", "tofu"),
    [
        (None, True),  # unset: pin self-signed certs on first use
        ("", True),
        ("tofu", True),
        ("false", False),  # explicit false stays "no verification at all"
        ("true", False),
        ("/etc/ssl/ca-bundle.pem", False),
    ],
)
def test_session_relevance_tofu_follows_bes_ssl_verify(monkeypatch, raw, tofu):
    monkeypatch.setenv("BES_ROOT_SERVER", "https://bes.example.com:52311")
    monkeypatch.setenv("BES_USER_NAME", "envuser")
    monkeypatch.setenv("BES_PASSWORD", "envpass")
    if raw is not None:
        monkeypatch.setenv("BES_SSL_VERIFY", raw)
    assert connection.load_config().tofu is tofu


def test_tofu_from_config_file(monkeypatch, tmp_path):
    path = write_conf(
        tmp_path / "besapi.conf",
        "[besapi]\nBES_ROOT_SERVER = https://bes.example.com:52311\n"
        "BES_USER_NAME = fileuser\nBES_PASSWORD = filepass\nBES_SSL_VERIFY = tofu\n",
    )
    monkeypatch.setattr(connection, "CONFIG_PATHS", [path])
    config = connection.load_config()
    assert config.tofu is True
    assert config.verify is False


def test_no_stdout_output(monkeypatch, tmp_path, capsys):
    """Stdout belongs to the MCP stdio transport; config loading must be silent."""
    conf = write_conf(
        tmp_path / "besapi.conf",
        "[besapi]\nBES_ROOT_SERVER = https://file.example.com:52311\n"
        "BES_USER_NAME = fileuser\nBES_PASSWORD = filepass\n",
    )
    monkeypatch.setattr(connection, "CONFIG_PATHS", [conf])
    connection.load_config()
    assert capsys.readouterr().out == ""


def test_source_has_no_print_calls():
    """Guard: no print() anywhere in the package (stdio transport safety)."""
    import pathlib

    import bigfix_root_mcp

    package_dir = pathlib.Path(bigfix_root_mcp.__file__).parent
    for source_file in package_dir.glob("*.py"):
        source = source_file.read_text()
        assert "print(" not in source, f"print() call found in {source_file.name}"


def test_reset_connection(monkeypatch):
    import bigfix_root_mcp.connection as connection_module

    sentinel = object()
    monkeypatch.setattr(connection_module, "_conn", sentinel)
    assert connection_module.get_connection() is sentinel
    connection_module.reset_connection()
    monkeypatch.setattr(
        connection_module,
        "load_config",
        lambda: (_ for _ in ()).throw(connection.ConnectionConfigError("no config")),
    )
    with pytest.raises(connection.ConnectionConfigError):
        connection_module.get_connection()
