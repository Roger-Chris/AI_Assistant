"""Gmail ingestion against a fake service."""
import asyncio
import base64

from sqlmodel import Session, select

from app import gmail
from app.db import Message, engine
from app.gmail import email_prefilter


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def mail(mid, frm, subject, body, ts=1789750000, unsubscribe=False, html_body=None):
    headers = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if unsubscribe:
        headers.append({"name": "List-Unsubscribe", "value": "<mailto:x>"})
    if html_body:
        payload = {"mimeType": "multipart/alternative", "headers": headers,
                   "parts": [{"mimeType": "text/html", "body": {"data": b64(html_body)}}]}
    else:
        payload = {"mimeType": "text/plain", "headers": headers, "body": {"data": b64(body)}}
    return {"id": mid, "internalDate": str(ts * 1000), "snippet": (body or "")[:50], "payload": payload}


class Req:
    def __init__(self, v):
        self.v = v

    def execute(self):
        return self.v


class FakeGmail:
    def __init__(self, msgs):
        self.msgs = {m["id"]: m for m in msgs}
        self.queries = []

    def users(self):
        return self

    def getProfile(self, userId):
        return Req({"emailAddress": "roger@example.com"})

    def messages(self):
        return self

    def list(self, userId, q, maxResults, pageToken=None):
        self.queries.append(q)
        return Req({"messages": [{"id": i} for i in self.msgs]})

    def get(self, userId, id, format):
        return Req(self.msgs[id])


def test_email_prefilter():
    assert email_prefilter("no-reply@shop.com", "Weekend deals", "buy now", True) == "email-bulk"
    assert email_prefilter("no-reply@bank.com", "Your credit card bill of ₹4,320 is due on 25 Sep", "...", True) is None
    assert email_prefilter("prof@college.edu", "Submit the form", "by friday", False) is None


def test_poll_stores_filters_and_classifies(client, fake_llm, monkeypatch):
    fake = FakeGmail([
        mail("m1", "HDFC <alerts@hdfcbank.net>", "Credit card statement: ₹4,320 due 25 Sep",
             "Your total amount due is Rs 4,320. Due date 25/09/2026.", unsubscribe=True),
        mail("m2", "Deals <no-reply@zomato.com>", "50% off tonight", "Order now", unsubscribe=True),
        mail("m3", "PALS <events@pals.org>", "Orientation confirmed", None, html_body="<p>See you <b>22 Sep</b> at 1:30 PM</p>"),
    ])
    monkeypatch.setitem(gmail._service_override, "primary", fake)
    fake_llm.script["credit card statement"] = {"action": "new", "kind": "task", "title": "Pay HDFC credit card ₹4,320",
                                               "due_at": "2026-09-25T18:00:00+05:30", "priority": "high", "confidence": 0.95}
    fake_llm.script["orientation confirmed"] = {"action": "new", "kind": "event", "title": "PALS orientation",
                                               "due_at": "2026-09-22T13:30:00+05:30", "confidence": 0.9}
    with Session(engine) as s:
        r = asyncio.run(gmail.poll_account(s, "primary"))
    assert r == {"stored": 3, "filtered": 1, "classified": 2}
    assert "-category:promotions" in fake.queries[0]
    with Session(engine) as s:
        rows = {m.external_id: m for m in s.exec(select(Message).where(Message.source == "email")).all()}
    assert rows["gmail:primary:m2"].classification == '{"kind":"noise","reason":"prefilter:email-bulk"}'
    assert rows["gmail:primary:m3"].text.startswith("Subject: Orientation confirmed")
    assert "See you 22 Sep at 1:30 PM" in rows["gmail:primary:m3"].text  # html stripped
    titles = {t["title"] for t in client.get("/tasks").json()}
    assert "Pay HDFC credit card ₹4,320" in titles and "PALS orientation" in titles
    # the classifier was told it was email
    assert "email to" in fake_llm.calls[-1][-1]["content"]
    # second poll: nothing new
    with Session(engine) as s:
        assert asyncio.run(gmail.poll_account(s, "primary"))["stored"] == 0
    # leave the shared test db as we found it
    with Session(engine) as s:
        from app.db import Chat, Note, Task
        for t in s.exec(select(Task).where(Task.chat_id.like("email:%"))).all():  # type: ignore[union-attr]
            s.delete(t)
        for m in s.exec(select(Message).where(Message.source == "email")).all():
            s.delete(m)
        for c in s.exec(select(Chat).where(Chat.chat_id.like("email:%"))).all():  # type: ignore[union-attr]
            s.delete(c)
        n = s.get(Note, "gmail_last_primary")
        if n:
            s.delete(n)
        s.commit()


def test_bulk_gate_keeps_mine_drops_marketing():
    """Real subjects from the first live poll."""
    drop = [
        ("Ananya Bhatt <ananya@unstop.com>", "10,000+ global courses just went flat-rate!", "Coursera Plus Offer"),
        ("Google Cloud <cloudplatform-noreply@google.com>", "[Reminder] [Action Required] Transition your scripts and workflows to gcloud storage",
         "gsutil is deprecated. Complete the migration before March 31, 2027."),
        ("LinkedIn <notifications-noreply@linkedin.com>", "Kishore S recently posted", "shared a post"),
        ("Indeed <alert@indeed.com>", "Project Lead @ Darsis software", "Your background in engineering"),
        ("Unstop <no-reply@unstop.com>", "Earn stipend up to INR 25,000 per month!", "Recommended Opportunities"),
    ]
    for sender, subject, body in drop:
        assert email_prefilter(sender, subject, body, True) == "email-bulk", subject
    keep = [
        ("HDFC <alerts@hdfcbank.net>", "Credit card statement", "Total amount due Rs 4,320 by 25/09"),
        ("Unstop <no-reply@unstop.com>", "Registration Complete | Amazon ML Challenge 2026", "Your registration is confirmed"),
        ("SheerID <verify@sheerid.com>", "Get verified for AWS Builder Center", "Complete your verification"),
        ("Airtel <billing@airtel.com>", "Your e-bill for September", "Amount payable 599"),
    ]
    for sender, subject, body in keep:
        assert email_prefilter(sender, subject, body, True) is None, subject


def test_confirmation_from_another_sender_completes_the_task(client, fake_llm, monkeypatch):
    """Register with one sender, get 'thanks for registering' from another -> complete."""
    ask = FakeGmail([mail("r1", "Amazon ML <noreply@unstop.com>", "Complete your registration | Amazon ML Challenge",
                          "Your registration is pending. Finish it by 25 Sep.")])
    fake_llm.script["complete your registration"] = {"action": "new", "kind": "task",
                                                     "title": "Finish Amazon ML Challenge registration",
                                                     "confidence": 0.95}
    monkeypatch.setitem(gmail._service_override, "reg", ask)
    with Session(engine) as s:
        asyncio.run(gmail.poll_account(s, "reg"))
    tid = [t for t in client.get("/tasks").json() if t["title"] == "Finish Amazon ML Challenge registration"][0]["id"]

    done = FakeGmail([mail("r2", "Unstop Team <team@unstop.in>", "Registered successfully for Amazon ML Challenge 2026",
                           "Thank you for registering. Your registration is confirmed.", ts=1789760000)])
    fake_llm.script["registered successfully"] = {"action": "complete", "task_id": tid, "confidence": 0.95}
    monkeypatch.setitem(gmail._service_override, "reg2", done)
    with Session(engine) as s:
        asyncio.run(gmail.poll_account(s, "reg2"))
    # the second sender saw the first sender's task in its candidate list
    assert f"#{tid}" in fake_llm.calls[-1][-1]["content"]
    assert [t for t in client.get("/tasks?status=done").json() if t["id"] == tid]
