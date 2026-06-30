import os

from watcher.config import _parse_env_assignment, load_dotenv


def test_parse_env_assignment_accepts_standard_and_powershell_forms():
    assert _parse_env_assignment("SMTP_USER=youraddress@gmail.com") == (
        "SMTP_USER",
        "youraddress@gmail.com",
    )
    assert _parse_env_assignment('$env:SMTP_APP_PASSWORD = "abcdefghijklmnop"') == (
        "SMTP_APP_PASSWORD",
        "abcdefghijklmnop",
    )
    assert _parse_env_assignment("export WATCHER_SEND_EMAIL=1 # live send") == (
        "WATCHER_SEND_EMAIL",
        "1",
    )
    assert _parse_env_assignment("# comment only") is None


def test_load_dotenv_sets_missing_values_without_overriding_existing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "SMTP_USER=from-file@gmail.com",
                '$env:SMTP_APP_PASSWORD = "from-file-password"',
                "EMAIL_TO=to-file@gmail.com",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SMTP_USER", "already-set@gmail.com")
    for key in ("SMTP_APP_PASSWORD", "EMAIL_TO"):
        monkeypatch.delenv(key, raising=False)

    load_dotenv(env_path)

    assert os.environ["SMTP_USER"] == "already-set@gmail.com"
    assert os.environ["SMTP_APP_PASSWORD"] == "from-file-password"
    assert os.environ["EMAIL_TO"] == "to-file@gmail.com"
