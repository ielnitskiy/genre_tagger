import os
import signal

import pytest

from src import main as main_module
from src.config import Config


def test_resolve_log_level_defaults_to_info(monkeypatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    assert main_module._resolve_log_level() == 20  # logging.INFO


def test_resolve_log_level_accepts_valid_level(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert main_module._resolve_log_level() == 10  # logging.DEBUG


def test_resolve_log_level_falls_back_to_info_on_garbage(monkeypatch, capsys):
    monkeypatch.setenv("LOG_LEVEL", "NOT_A_LEVEL")
    assert main_module._resolve_log_level() == 20  # logging.INFO
    assert "NOT_A_LEVEL" in capsys.readouterr().err


def test_sleep_interruptibly_stops_early_when_flag_already_set():
    stop = {"requested": True}
    # Не должен спать вовсе — цикл проверяет флаг перед каждым time.sleep.
    main_module._sleep_interruptibly(1000.0, stop)


def test_sleep_interruptibly_stops_after_flag_set_mid_sleep(monkeypatch):
    stop = {"requested": False}
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) == 2:
            stop["requested"] = True

    monkeypatch.setattr(main_module.time, "sleep", fake_sleep)
    main_module._sleep_interruptibly(10.0, stop)

    assert len(calls) == 2  # остановились сразу после второго тика, не проспав все 10с


def test_daemon_loop_stops_after_current_scan_pass_on_sigterm(tmp_path, monkeypatch):
    config = Config(
        music_dir=str(tmp_path / "music"),
        db_path=str(tmp_path / "genres.db"),
        lastfm_api_key="key",
        scan_interval_seconds=10_000,  # заведомо больше, чем должен реально проспать тест
        min_tag_count=1,
        max_genres=1,
        genre_ttl_days=180,
        skip_dirs=frozenset(),
    )
    (tmp_path / "music").mkdir()

    run_once_calls = []

    def fake_run_once(*_args, **_kwargs):
        run_once_calls.append(1)
        os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(main_module, "load_config", lambda: config)
    monkeypatch.setattr(main_module, "LastfmClient", lambda *a, **kw: object())
    monkeypatch.setattr(main_module, "run_once", fake_run_once)
    monkeypatch.setattr("sys.argv", ["genre-tagger"])

    main_module.main()

    assert run_once_calls == [1]  # цикл не начал второй проход и не залип в _sleep_interruptibly


@pytest.fixture(autouse=True)
def _restore_default_signal_handlers():
    yield
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.default_int_handler)
