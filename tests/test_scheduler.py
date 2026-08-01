import datetime
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ytlive_snapshot import capture


class FakeScheduler:
    def __init__(self):
        self.state = capture.STATE_STOPPED
        self.jobs = []

    def scheduled_job(self, trigger, **options):
        self.noon_options = {"trigger": trigger, **options}

        def decorator(func):
            return func

        return decorator

    def add_job(self, func, trigger, **options):
        self.jobs.append({"func": func, "trigger": trigger, **options})

    def start(self):
        raise KeyboardInterrupt


class SchedulerConfigurationTest(unittest.TestCase):
    def test_scheduled_captures_allow_short_misfires(self):
        scheduler = FakeScheduler()
        config = {
            "schedule": {
                "timezone": "Asia/Tokyo",
                "sunset_offset_minutes": 40,
                "misfire_grace_time_sec": 300,
                "enable_noon": True,
                "enable_sunset": True,
            }
        }

        def fake_sun(observer, date, tzinfo):
            return {
                "sunset": datetime.datetime.now(tzinfo)
                + datetime.timedelta(hours=1)
            }

        with patch.object(
            capture, "BlockingScheduler", return_value=scheduler
        ), patch.object(
            capture, "sun", side_effect=fake_sun
        ), patch.object(
            capture,
            "WEBCAM_LOCATION",
            SimpleNamespace(observer=object()),
        ):
            capture.schedule_capture_jobs(config)

        jobs = {job["id"]: job for job in scheduler.jobs}
        self.assertEqual(jobs["fixed_capture_noon"]["misfire_grace_time"], 300)
        self.assertTrue(jobs["fixed_capture_noon"]["coalesce"])
        self.assertEqual(jobs["sunset_capture"]["misfire_grace_time"], 300)
        self.assertTrue(jobs["sunset_capture"]["coalesce"])

    def test_multiple_fixed_times_are_registered(self):
        scheduler = FakeScheduler()
        config = {
            "schedule": {
                "timezone": "Asia/Tokyo",
                "fixed_times": [
                    {"id": "morning", "time": "08:15", "prefix": "morning"},
                    {"id": "noon", "time": "12:00", "prefix": "noon"},
                    {"id": "afternoon", "time": "15:30", "prefix": "afternoon"},
                ],
                "enable_sunset": False,
            }
        }

        with patch.object(capture, "BlockingScheduler", return_value=scheduler):
            capture.schedule_capture_jobs(config)

        jobs = {job["id"]: job for job in scheduler.jobs}
        self.assertEqual(jobs["fixed_capture_morning"]["hour"], 8)
        self.assertEqual(jobs["fixed_capture_morning"]["minute"], 15)
        self.assertEqual(jobs["fixed_capture_noon"]["hour"], 12)
        self.assertEqual(jobs["fixed_capture_afternoon"]["minute"], 30)

    def test_invalid_fixed_time_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid time"):
            capture._configured_fixed_times(
                {
                    "fixed_times": [
                        {"id": "bad", "time": "25:00", "prefix": "bad"},
                    ]
                }
            )


if __name__ == "__main__":
    unittest.main()
