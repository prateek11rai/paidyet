import pytest

from paidyet.config import ROOT, ConfigError, load_settings, parse_allowed_users


def test_parse_allowed_users():
    assert parse_allowed_users(" me:111 , arjun:222,") == {111: "me", 222: "arjun"}
    assert parse_allowed_users("") == {}


@pytest.mark.parametrize("raw", ["arjun", "arjun:abc", ":222", "arjun:"])
def test_parse_allowed_users_rejects_bad_pairs(raw):
    with pytest.raises(ConfigError):
        parse_allowed_users(raw)


def test_defaults_without_env():
    s = load_settings({})
    assert s.telegram_bot_token is None and s.sentry_dsn is None and s.admin_user_id is None
    assert s.ollama_model == "gemma4:e4b"
    assert s.ollama_url == "http://127.0.0.1:11434"
    assert s.data_dir == ROOT / ".data"
    assert str(s.tz) == "Asia/Kolkata"


def test_allowlist_and_names():
    s = load_settings({"ADMIN_USER_ID": "111", "ALLOWED_USERS": "Prateek:111,Arjun:222"})
    assert s.is_allowed(111) and s.is_allowed(222) and not s.is_allowed(333)
    assert s.is_admin(111) and not s.is_admin(222)
    assert s.name_of(111) == "Prateek"
    assert s.user_id_for("arjun") == 222
    assert s.user_id_for("rahul") is None


def test_admin_is_allowed_even_if_not_listed():
    s = load_settings({"ADMIN_USER_ID": "111"})
    assert s.is_allowed(111)
    assert s.name_of(111) == "the admin"


@pytest.mark.parametrize("host", ["0.0.0.0:11434", "192.168.1.5:11434", "example.com:11434"])
def test_ollama_must_stay_on_loopback(host):
    with pytest.raises(ConfigError):
        load_settings({"OLLAMA_HOST": host})


def test_secrets_are_not_in_repr():
    s = load_settings({"TELEGRAM_BOT_TOKEN": "123:secret-token", "SENTRY_DSN": "https://key@example.invalid/1"})
    assert "secret-token" not in repr(s)
    assert "key@" not in repr(s)


def test_bad_admin_id_and_tz():
    with pytest.raises(ConfigError):
        load_settings({"ADMIN_USER_ID": "arjun"})
    with pytest.raises(ConfigError):
        load_settings({"TZ": "Mars/Olympus"})
