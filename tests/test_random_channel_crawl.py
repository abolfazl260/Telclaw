"""Randomized multi-channel crawl order regression tests."""

import asyncio
from datetime import date

import pytest

import services.scheduler_service as scheduler_module
from services.crawler_service import CrawlerService
from services.scheduler_service import SchedulerService


class FakeCrawlJob:
    def __init__(self, stop_on=None):
        self.visits = []
        self.stop_on = stop_on

    async def run_channel(
        self, client, channel_username, from_date, to_date, *,
        crawl_mode, should_stop,
    ):
        self.visits.append(channel_username)
        return {"saved": 1, "stopped": channel_username == self.stop_on}


class FakeMonitor:
    def __init__(self):
        self.reports = []
        self.summaries = 0

    async def report(self, stage, payload):
        self.reports.append((stage, payload))

    async def broadcast_transfer_live(self):
        self.summaries += 1


class FakeStageControl:
    def __init__(self):
        self.consumed = []

    def is_skip_requested(self, stage):
        return False

    def consume_skip(self, stage):
        self.consumed.append(stage)


def build_scheduler(stop_on=None):
    # The loop under test needs no AI/provider initialization or Telegram connection.
    scheduler = object.__new__(SchedulerService)
    scheduler.crawl_job = FakeCrawlJob(stop_on=stop_on)
    scheduler.monitor = FakeMonitor()
    scheduler.stage_control = FakeStageControl()
    pipelines = []

    async def fake_post_crawl(client, channel, result):
        pipelines.append((channel, result))

    scheduler._run_post_crawl_pipeline = fake_post_crawl
    return scheduler, pipelines


@pytest.mark.asyncio
async def test_channel_order_is_reshuffled_each_cycle_without_changing_selection(monkeypatch):
    scheduler, pipelines = build_scheduler()
    selected = ["@alpha", "@beta", "@gamma"]
    original = selected.copy()
    orders = [
        ["@gamma", "@alpha", "@beta"],
        ["@beta", "@gamma", "@alpha"],
    ]
    shuffled = []
    delays = []
    cycles_finished = 0

    def fake_shuffle(items):
        # Check that the scheduler shuffles a new copy, never caller-owned list.
        assert items is not selected
        items[:] = orders[len(shuffled)]
        shuffled.append(items.copy())

    async def fake_sleep(seconds):
        nonlocal cycles_finished
        delays.append(seconds)
        if seconds > 30:
            cycles_finished += 1
            if cycles_finished == 2:
                raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.random, "shuffle", fake_shuffle)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await scheduler._run_cycle(
            None, selected, 1, date.today(), date.today(), "all",
            channel_interval_minutes=0.25,
        )

    assert shuffled == orders
    assert selected == original
    assert scheduler.crawl_job.visits == orders[0] + orders[1]
    assert [sorted(scheduler.crawl_job.visits[i:i + 3]) for i in (0, 3)] == [
        sorted(selected), sorted(selected),
    ]
    assert [round(seconds) for seconds in delays] == [15, 15, 60, 15, 15, 60]
    assert len(pipelines) == 2
    assert scheduler.monitor.summaries == 2
    # The existing reporter prefixes '@' even if a selected username has one;
    # normalize that legacy display detail, since this test covers order.
    assert [entry[1]["channel"].lstrip("@") for entry in scheduler.monitor.reports] == [
        "gamma", "alpha", "beta", "beta", "gamma", "alpha",
    ]


@pytest.mark.asyncio
async def test_random_order_preserves_early_stop_and_skips_incomplete_cycle(monkeypatch):
    scheduler, pipelines = build_scheduler(stop_on="@alpha")
    selected = ["@alpha", "@beta", "@gamma"]

    def fake_shuffle(items):
        items[:] = ["@gamma", "@alpha", "@beta"]

    async def fake_sleep(seconds):
        if seconds > 30:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.random, "shuffle", fake_shuffle)
    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await scheduler._run_cycle(
            None, selected, 1, date.today(), date.today(), "all",
        )

    assert scheduler.crawl_job.visits == ["@gamma", "@alpha"]
    assert selected == ["@alpha", "@beta", "@gamma"]
    assert pipelines == []
    assert scheduler.monitor.summaries == 0
    assert len(scheduler.monitor.reports) == 2


def test_category_selection_still_deduplicates_channels_before_scheduling():
    class FakeChannels:
        def categories(self):
            return ["jobs", "housing"]

        def channels_for_category(self, category):
            return {
                "jobs": [{"username": "@alpha"}, {"username": "@beta"}],
                "housing": [{"username": "@BETA"}, {"username": "@gamma"}],
            }[category]

    class FakeScheduler:
        def __init__(self):
            self.calls = []

        def schedule_cycle(self, client, channels, *args, **kwargs):
            self.calls.append(list(channels))
            return "scheduled"

    fake_scheduler = FakeScheduler()
    crawler = CrawlerService(FakeChannels(), fake_scheduler)
    jobs = crawler.schedule_categories(
        None, ["jobs", "housing"], date.today(), date.today(),
        channel_interval_minutes=0,
    )

    assert jobs == ["scheduled"]
    assert fake_scheduler.calls == [["@alpha", "@beta", "@gamma"]]


def test_canada_job_sources_include_user_requested_public_channels_without_duplicates():
    """Inventory check: source categories stay stable and case-insensitively unique."""
    from pathlib import Path
    import json

    file = Path(__file__).resolve().parents[1] / "channels.json"
    categories = json.loads(file.read_text(encoding="utf-8"))
    jobs = categories["Canada_Job"]
    usernames = [row["username"].casefold() for row in jobs if row.get("username")]
    assert len(usernames) == len(set(usernames))
    expected = {
        "torontohiring", "vancouveriranian",
        "employmentandemployersvancouver", "persianwomencareerads",
        "montrealhiring", "jobsfouryou", "jobs_toronto_ontario",
        "uzimiznikilar_news",
    }
    assert expected.issubset(set(usernames))
    assert usernames.count("torontohiring") == 1
    assert usernames.count("jobs_toronto_ontario") == 1
    private = [
        item for item in jobs
        if item.get("invite_link") == "https://t.me/+BlviDU3G7OPUZOpuFEEuKg"
    ]
    assert len(private) == 1
    assert not private[0].get("username")


def test_canada_job_private_invite_is_not_scheduled_for_crawling():
    from pathlib import Path
    import json

    categories = json.loads((Path(__file__).resolve().parents[1] / "channels.json").read_text())
    class Data:
        def channels_for_category(self, category):
            return categories[category]

    class FakeScheduler:
        def __init__(self):
            self.calls = []
        def schedule_cycle(self, client, channels, *args, **kwargs):
            self.calls.append(list(channels))
            return "scheduled"

    scheduler = FakeScheduler()
    crawler = CrawlerService(Data(), scheduler)
    result = crawler.schedule_categories(
        None, ["Canada_Job"], date.today(), date.today(), channel_interval_minutes=0,
    )
    assert result == ["scheduled"]
    channels = scheduler.calls[0]
    assert len(channels) == 14
    assert all(not channel.startswith("https://t.me/+") for channel in channels)
    assert "VancouverIranian" in channels
