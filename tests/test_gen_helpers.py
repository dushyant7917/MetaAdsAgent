import pytest

from metaads.config import load_settings
from metaads.ratelimit import RateLimiter

S = load_settings()


def test_rate_limiter_blocks_fifth_call_until_window_passes():
    now = [0.0]
    sleeps = []

    def sleep(sec):
        sleeps.append(sec)
        now[0] += sec

    rl = RateLimiter(4, clock=lambda: now[0], sleep=sleep)
    for _ in range(4):
        rl.acquire()
    assert sleeps == []
    rl.acquire()                      # 5th must wait for the first call to leave the 60 s window
    assert sum(sleeps) == pytest.approx(60.0, abs=0.1)
