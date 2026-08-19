import logging

import pytest

from coldstart.logging_setup import get_logger, new_run_id, setup_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        root.addHandler(handler)
    root.setLevel(original_level)


def _read_log(tmp_path) -> str:
    for handler in logging.getLogger().handlers:
        handler.flush()
    return (tmp_path / "coldstart.log").read_text()


def test_new_run_id_is_short_and_unique():
    a, b = new_run_id(), new_run_id()
    assert a != b
    assert 4 <= len(a) <= 12


def test_setup_logging_creates_log_dir_and_file(tmp_path):
    log_dir = tmp_path / "nested" / "logs"
    setup_logging(log_dir)
    assert log_dir.is_dir()
    assert (log_dir / "coldstart.log").exists()


def test_run_id_appears_in_emitted_records(tmp_path):
    setup_logging(tmp_path, run_id="testrun42")
    get_logger(__name__).info("hello world")
    content = _read_log(tmp_path)
    assert "testrun42" in content
    assert "hello world" in content


def test_level_respected_from_arg(tmp_path):
    setup_logging(tmp_path, level="WARNING")
    logger = get_logger(__name__)
    logger.debug("should not appear")
    logger.warning("should appear")
    content = _read_log(tmp_path)
    assert "should not appear" not in content
    assert "should appear" in content


def test_default_level_is_info(tmp_path):
    setup_logging(tmp_path)
    logger = get_logger(__name__)
    logger.debug("debug msg")
    logger.info("info msg")
    content = _read_log(tmp_path)
    assert "debug msg" not in content
    assert "info msg" in content


def test_writes_to_both_file_and_console(tmp_path, capsys):
    setup_logging(tmp_path)
    get_logger(__name__).info("visible everywhere")
    content = _read_log(tmp_path)
    assert "visible everywhere" in content
    assert "visible everywhere" in capsys.readouterr().err
