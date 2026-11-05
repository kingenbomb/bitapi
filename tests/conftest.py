# tests/e2e/ 不进默认收集:那里的用例要起真 uvicorn、占 127.0.0.1:8123、跑约 40 秒。
# collect_ignore 只在递归收集时生效,命令行直接点名 tests/e2e 仍然会跑 —— 这正是想要的:
#   python -m pytest tests/ -q        只跑单元与集成
#   python -m pytest tests/e2e -q     显式跑端到端
import pytest
import os
import tempfile
import pathlib
import sys
import gc

# Module collection can import pool_state before a test module sets its own DB.
# Install the fallback before collection so no ordering ever reaches the project DB.
_DATABASES = tempfile.TemporaryDirectory(prefix="bitapi-pytest-")
os.environ["BITAPI_DB"] = str(pathlib.Path(_DATABASES.name) / "isolated.db")

collect_ignore = ["e2e"]


@pytest.fixture(autouse=True)
def _reset_auth_throttle():
    from core import throttle
    throttle.reset_all()
    yield
    throttle.reset_all()


def pytest_sessionfinish(session):
    root = pathlib.Path(_DATABASES.name).resolve()
    # Test modules may temporarily replace the singleton, leaving its thread
    # connections alive after the original DB object loses that reference.
    from core.db import ThreadConns
    for obj in gc.get_objects():
        if isinstance(obj, ThreadConns) and pathlib.Path(obj.path).resolve().is_relative_to(root):
            with obj._lock:
                for conn in obj._live.values():
                    conn.close()
                obj._live.clear()
    _DATABASES.cleanup()


@pytest.fixture(autouse=True, scope="session")
def _disable_auth_throttle():
    """整套单元/集成测试跑在一个进程里,TestClient 的来源 IP 永远是同一个 —— 几十个
    用例各注册几个用户,按生产阈值(每 IP 每小时 5 个)第六个就 429,而且是「前面哪个
    模块先跑」决定谁被拦。所以套件级把登录/注册限速关掉;限速本身的门禁在
    tests/test_auth_throttle.py,那里显式设阈值再恢复。"""
    from core import throttle
    limiters = (throttle.LOGIN_IP, throttle.LOGIN_EMAIL, throttle.REGISTER_IP,
                throttle.RESET_EMAIL, throttle.RESET_IP,
                throttle.REGISTER_MAIL_IP, throttle.REGISTER_MAIL_EMAIL)
    saved = [lim.limit for lim in limiters]
    for lim in limiters:
        lim.limit = 0
    yield
    for lim, val in zip(limiters, saved):
        lim.limit = val
