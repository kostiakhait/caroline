"""Status polls that answered fine are left out of uvicorn's access log (app/quiet_access_log.py, 2026-11-22).
Run: python -m pytest test_quiet_access_log.py -q"""

import logging

import uvicorn

from app import quiet_access_log as q


def record(method, path, status):
    return logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                             ("127.0.0.1:5", method, path, "1.1", status), None)


def test_only_successful_status_polls_are_dropped():
    f = q.QuietPollFilter()
    assert not f.filter(record("GET", "/api/status", 200))
    assert not f.filter(record("GET", "/api/update/status", 200))
    assert not f.filter(record("GET", "/status?verbose=1", 200))
    assert f.filter(record("GET", "/api/status", 503))           # a failing poll is still logged
    assert f.filter(record("POST", "/api/message", 200))
    assert f.filter(record("GET", "/api/statusx", 200))
    assert f.filter(logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "other %s", ("x",), None))


def test_install_is_idempotent_and_survives_uvicorn_logging_setup():
    q.install()
    q.install()
    uvicorn.Config(lambda scope, receive, send: None, log_level="info")   # what uvicorn.run does before serving
    filters = [f for f in logging.getLogger("uvicorn.access").filters if isinstance(f, q.QuietPollFilter)]
    assert len(filters) == 1
