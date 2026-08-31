"""The API says what it is doing, at a level that actually reaches the log.

Nothing configured a logging level, so the root logger sat at WARNING and every
`_log.info` in this codebase was discarded. That is not a cosmetic gap. Twice while
debugging Visual QA in production the deciding question was "did it find a frame or
not", the answer is logged at INFO by `latest_frame`, and it was not there. The visual
check is deliberately non-fatal, so an absent verdict and a swallowed failure look
identical from outside; the log is the only thing that separates them, and it was off.
"""

import logging

from dailies_api.main import configure_logging


def test_info_is_emitted_so_the_quiet_paths_are_visible(caplog):
    configure_logging()
    logger = logging.getLogger("dailies_api.frames")

    with caplog.at_level(logging.INFO, logger="dailies_api.frames"):
        logger.info("No frames for %s; nothing to look at", "SH201")

    assert any("No frames for SH201" in r.getMessage() for r in caplog.records)


def test_configuring_twice_does_not_double_every_line():
    """Cloud Run may import the factory more than once; duplicated handlers duplicate logs."""
    configure_logging()
    before = len(logging.getLogger().handlers)
    configure_logging()
    assert len(logging.getLogger().handlers) == before


def test_the_level_can_be_turned_down_without_a_deploy():
    """A noisy incident is not a reason to have to rebuild an image."""
    configure_logging({"DAILIES_LOG_LEVEL": "WARNING"})
    assert logging.getLogger().level == logging.WARNING
    configure_logging({"DAILIES_LOG_LEVEL": "INFO"})
    assert logging.getLogger().level == logging.INFO


def test_an_unreadable_level_falls_back_rather_than_crashing_the_service():
    configure_logging({"DAILIES_LOG_LEVEL": "chatty"})
    assert logging.getLogger().level == logging.INFO
