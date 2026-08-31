import scheduler


class FakeScheduler:
    def __init__(self):
        self.jobs = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append((func, trigger, kwargs))


def test_market_jobs_are_weekday_only_and_keep_market_argument():
    fake = FakeScheduler()

    scheduler._add_market_jobs(fake, "KR", ["16:10"], scheduler.KST)
    scheduler._add_market_jobs(fake, "US", ["16:30"], scheduler.NEW_YORK)

    assert [job[2]["args"] for job in fake.jobs] == [["KR"], ["US"]]
    assert [job[2]["id"] for job in fake.jobs] == [
        "scan_kr_1610", "scan_us_1630"
    ]
    assert [str(job[1].timezone) for job in fake.jobs] == [
        "Asia/Seoul", "America/New_York"
    ]
    assert all("day_of_week='mon-fri'" in str(job[1]) for job in fake.jobs)


def test_run_scans_only_requested_market(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "scanner.scan_engine.run_scan",
        lambda **kwargs: calls.append(kwargs),
    )

    scheduler._run("US")

    assert calls == [{"market": "US", "triggered_by": "scheduler"}]
