"""Health: hub outage clock, nothing-can-reach-you, disk and db size, dead-man ping, /health/detail."""
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlmodel import Session, select

from app import alerts, health
from app.config import settings
from app.db import Alert, engine

IST = ZoneInfo("Asia/Kolkata")


def ist(h, m=0, day=29):
    return datetime(2026, 10, day, h, m, tzinfo=IST)


class Net:
    """Fake hub (/api/health) and healthchecks.io (/ping/...) on one MockTransport."""

    def __init__(self):
        self.hub_up = True
        self.pings: list[str] = []
        self.hub_auth: list[str] = []

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.host == "hub":
            assert req.url.path == "/api/health"
            self.hub_auth.append(req.headers.get("authorization", ""))
            if not self.hub_up:
                raise httpx.ConnectError("refused", request=req)
            return httpx.Response(200, json={"ok": True})
        if req.url.host == "hc.example":
            self.pings.append(req.url.path)
            return httpx.Response(200, text="OK")
        raise AssertionError(f"unexpected request {req.url}")


@pytest.fixture
def net(monkeypatch):
    n = Net()
    health.transport = httpx.MockTransport(n.handle)
    health.reset()
    monkeypatch.setattr(settings, "hub_url", "http://hub:4317")
    monkeypatch.setattr(settings, "orc_token", "orc-test")
    monkeypatch.setattr(settings, "ntfy_topic", "")
    monkeypatch.setattr(settings, "healthcheck_url", "https://hc.example/ping/test-uuid")
    monkeypatch.setattr(settings, "health_min_free_gb", 0.0)
    monkeypatch.setattr(settings, "health_max_db_mb", 100_000)
    # alerts raised while the hub is "down" must not try to reach a real hub
    alerts.hub_transport = httpx.MockTransport(lambda r: httpx.Response(503))
    yield n
    health.transport = None
    alerts.hub_transport = None
    health.reset()
    with Session(engine) as s:
        for a in s.exec(select(Alert).where(Alert.kind == "health")).all():
            s.delete(a)
        s.commit()


def health_alerts():
    with Session(engine) as s:
        return list(s.exec(select(Alert).where(Alert.kind == "health")).all())


def by_name(checks):
    return {c.name: c for c in checks}


async def test_all_well_pings_the_dead_man(net):
    checks = by_name(await health.run(now=ist(10)))
    assert checks["hub"].ok and checks["reach"].ok and checks["disk"].ok and checks["db"].ok
    assert net.pings == ["/ping/test-uuid"]
    assert net.hub_auth == ["Bearer orc-test"]
    assert not health_alerts()


async def test_hub_alert_only_after_30_minutes_and_once_a_day(net, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "t")  # a fallback exists, so "reach" is fine
    alerts.ntfy_transport = httpx.MockTransport(lambda r: httpx.Response(200))
    try:
        net.hub_up = False
        c = by_name(await health.run(now=ist(10)))["hub"]
        assert not c.ok and not c.alert
        await health.run(now=ist(10, 20))
        assert not health_alerts()
        c = by_name(await health.run(now=ist(10, 31)))["hub"]
        assert c.alert and "31 min" in c.detail
        [a] = health_alerts()
        assert a.key == "health:hub:2026-10-29" and a.level == alerts.NOTIFY and a.channel == "ntfy"
        with Session(engine) as s:  # you said "not important": still not raised again today
            alerts.ack(s, s.get(Alert, a.id), "dismiss")
        await health.run(now=ist(11))
        assert len(health_alerts()) == 1
        await health.run(now=ist(10, 31, day=30))  # a new day, still down
        assert len(health_alerts()) == 2
    finally:
        alerts.ntfy_transport = None


async def test_hub_back_resets_the_clock_and_closes_the_alert(net, monkeypatch):
    monkeypatch.setattr(settings, "ntfy_topic", "t")
    alerts.ntfy_transport = httpx.MockTransport(lambda r: httpx.Response(200))
    try:
        net.hub_up = False
        await health.run(now=ist(10))
        await health.run(now=ist(10, 40))
        [a] = health_alerts()
        assert a.status == "sent"
        net.hub_up = True
        await health.run(now=ist(10, 45))
        with Session(engine) as s:
            assert s.get(Alert, a.id).status == "done"
        net.hub_up = False
        c = by_name(await health.run(now=ist(10, 50)))["hub"]
        assert "0 min" in c.detail and not c.alert
    finally:
        alerts.ntfy_transport = None


async def test_nothing_can_reach_you_fails_the_dead_man_ping(net, caplog):
    net.hub_up = False
    checks = by_name(await health.run(now=ist(10)))
    assert not checks["reach"].ok and "nothing can reach you" in checks["reach"].detail
    assert net.pings == ["/ping/test-uuid/fail"]
    assert "NOTHING CAN REACH THE OWNER" in caplog.text


async def test_reach_without_hub_url(net, monkeypatch):
    monkeypatch.setattr(settings, "hub_url", "")
    checks = by_name(await health.collect(now=ist(10)))
    assert "hub" not in checks and not checks["reach"].ok
    monkeypatch.setattr(settings, "ntfy_topic", "x")
    assert by_name(await health.collect(now=ist(10)))["reach"].ok


async def test_disk_and_db_limits_raise_alerts(net, monkeypatch):
    monkeypatch.setattr(settings, "health_min_free_gb", 1e9)
    monkeypatch.setattr(settings, "health_max_db_mb", 0)
    checks = by_name(await health.run(now=ist(10)))
    assert not checks["disk"].ok and "GB free" in checks["disk"].detail
    assert not checks["db"].ok and "MB" in checks["db"].detail
    keys = sorted(a.key for a in health_alerts())
    assert keys == ["health:db:2026-10-29", "health:disk:2026-10-29"]


async def test_no_healthcheck_url_no_ping(net, monkeypatch):
    monkeypatch.setattr(settings, "healthcheck_url", "")
    await health.run(now=ist(10))
    assert net.pings == []


def test_detail_endpoint(client, net):
    assert client.get("/health/detail", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").json() == {"ok": True}  # the plain one stays open
    body = client.get("/health/detail").json()
    assert body["ok"] is True and body["channels"] == ["hub"] and body["deadman"] is True
    assert {c["name"] for c in body["checks"]} == {"hub", "reach", "disk", "db"}
    assert net.pings == []  # looking does not ping or alert


def test_detail_reports_a_failing_check(client, net, monkeypatch):
    monkeypatch.setattr(settings, "health_max_db_mb", 0)
    body = client.get("/health/detail").json()
    assert body["ok"] is False
    assert next(c for c in body["checks"] if c["name"] == "db")["ok"] is False
    assert not health_alerts()



async def test_backup_status(net, monkeypatch):
    stamp = health._db_path().parent / "backup-status"
    monkeypatch.setattr(settings, "backup_repo", "")
    assert "backup" not in by_name(await health.collect(now=ist(10)))
    monkeypatch.setattr(settings, "backup_repo", "rclone:gdrive:assistant-backup")
    try:
        stamp.unlink(missing_ok=True)
        assert by_name(await health.collect(now=ist(10)))["backup"].ok  # nothing has run yet
        stamp.write_text("ok 2026-10-28T22:15:00Z\n")  # 03:45 IST on the 29th
        assert by_name(await health.collect(now=ist(10)))["backup"].ok
        assert not by_name(await health.collect(now=ist(10, day=31)))["backup"].ok  # 2+ days old
        stamp.write_text("failed 2026-10-28T22:16:00Z\n")
        checks = by_name(await health.run(now=ist(10)))
        assert not checks["backup"].ok and "failed" in checks["backup"].detail
        assert [a.key for a in health_alerts()] == ["health:backup:2026-10-29"]
    finally:
        stamp.unlink(missing_ok=True)
