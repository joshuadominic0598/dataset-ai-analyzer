import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import patch

from reviso.rate_limits import RateGate, RateLimiter, RateLimitError, seconds

class RateLimitTests(unittest.TestCase):
    def test_duration_and_retry_after_formats(self):
        self.assertEqual(seconds("1m2.5s"), 62.5)
        self.assertEqual(seconds("250ms"), 0.25)
        self.assertEqual(seconds("3h1m"), 10860)
        self.assertEqual(seconds("-1"), 0)
        for value in (None, "nan", "inf", "3sjunk", "not a date"):
            self.assertIsNone(seconds(value))
        value = format_datetime(datetime.fromtimestamp(1060, timezone.utc), usegmt=True)
        with patch("reviso.rate_limits.time.time", return_value=1000):
            self.assertEqual(seconds(value), 60)

    def test_retry_after_is_not_shortened_and_a_wait_is_visible(self):
        gate = RateGate(0)
        events = []
        with (
            patch("reviso.rate_limits.time.monotonic", return_value=100),
            patch("reviso.rate_limits.time.sleep") as sleep,
        ):
            gate.defer({"Retry-After": "60"}, 0)
            gate.wait(1000, 280, lambda kind, detail: events.append((kind, detail)))
        sleep.assert_called_once_with(60.25)
        self.assertEqual(events[0][0], "rate_limit_wait")
        self.assertEqual(events[0][1]["reason"], "retry")

    def test_missing_retry_header_uses_bounded_exponential_backoff(self):
        gate = RateGate(0)
        with patch("reviso.rate_limits.time.monotonic", return_value=100):
            for attempt in range(3):
                gate.defer({}, attempt)
                self.assertEqual(gate.cooldown, 100 + 15 * 2**attempt + 0.25)

    def test_long_daily_wait_stops_without_sleeping_or_retrying_early(self):
        gate = RateGate(0)
        with (
            patch("reviso.rate_limits.time.monotonic", return_value=100),
            patch("reviso.rate_limits.time.sleep") as sleep,
        ):
            gate.defer({"retry-after": "86400"}, 0)
            with self.assertRaisesRegex(RateLimitError, "longer wait"):
                gate.wait(1000, 280, None)
        sleep.assert_not_called()

    def test_remaining_tokens_are_checked_before_the_next_request(self):
        gate = RateGate(0)
        with (
            patch("reviso.rate_limits.time.monotonic", return_value=100),
            patch("reviso.rate_limits.time.sleep") as sleep,
        ):
            gate.update(
                {
                    "X-RateLimit-Remaining-Tokens": "10",
                    "X-RateLimit-Limit-Tokens": "8000",
                    "X-RateLimit-Reset-Tokens": "1m2s",
                }
            )
            gate.wait(1000, 280, None)
        sleep.assert_called_once_with(62.25)

    def test_remaining_requests_are_checked_and_expired_limits_do_not_block(self):
        gate = RateGate(0)
        with patch("reviso.rate_limits.time.monotonic", return_value=100):
            gate.update(
                {"x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "20s"}
            )
        with (
            patch("reviso.rate_limits.time.monotonic", return_value=121),
            patch("reviso.rate_limits.time.sleep") as sleep,
        ):
            gate.wait(10, 280, None)
        sleep.assert_not_called()

    def test_connection_queue_is_shared_across_models_without_storing_raw_keys(self):
        limiter = RateLimiter(interval=0)
        with limiter.guard("groq", "private-test-key", "model-a", time.monotonic() + 10) as first:
            self.assertTrue(first.lock.locked())
        with limiter.guard("groq", "private-test-key", "model-b", time.monotonic() + 10) as second:
            self.assertIs(first, second)
        self.assertNotIn("private-test-key", repr(limiter.gates))
        with limiter.guard("groq", "different-key", "model-a", time.monotonic() + 10) as other:
            self.assertIsNot(first, other)

    def test_simultaneous_calls_use_a_bounded_queue(self):
        limiter = RateLimiter(interval=0)
        events = []
        with limiter.guard("groq", "key", "a", time.monotonic() + 10):

            def queued():
                with self.assertRaisesRegex(RateLimitError, "queue"):
                    with limiter.guard(
                        "groq", "key", "b", 0, lambda kind, detail: events.append(kind)
                    ):
                        self.fail("A simultaneous request entered the same connection.")

            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(queued).result(timeout=1)
        self.assertEqual(events, ["rate_limit_wait"])

if __name__ == "__main__":
    unittest.main()
