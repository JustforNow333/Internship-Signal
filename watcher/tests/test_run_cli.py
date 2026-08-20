"""Watcher command-line startup behavior."""

from watcher.cli import main as watcher_main
from watcher.config import WatcherConfig


def test_main_logs_startup_and_total_runtime_stages(tmp_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="watcher.run")
    config = WatcherConfig(companies=())
    sentinel = object()
    monkeypatch.setattr("watcher.cli.load_watchlist", lambda _path: config)
    monkeypatch.setattr("watcher.cli.email_sending_enabled", lambda: False)
    monkeypatch.setattr("watcher.cli.load_health_alert_policy", lambda: None)
    monkeypatch.setattr("watcher.cli.run_once", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr("watcher.cli.print_report", lambda result: None)
    monkeypatch.setattr("watcher.cli.print_heartbeat", lambda result: None)

    exit_code = watcher_main(
        [
            "--watchlist",
            str(tmp_path / "unused.yml"),
            "--seen-db",
            str(tmp_path / "seen.sqlite"),
        ]
    )

    assert exit_code == 0
    assert [
        record.getMessage().split("stage=", 1)[1].split(" ", 1)[0]
        for record in caplog.records
        if record.getMessage().startswith("STAGE-TIMING ")
    ] == ["configuration_startup", "watcher_runtime"]
