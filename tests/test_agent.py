"""Offline tests for the AI navigator's guard rails, approvals and the bot's
two-phase execution. No browser, no network: the page and Claude are faked."""
import threading
import time
from types import SimpleNamespace as NS

from arb.agent import BetAgent
from arb.approvals import ApprovalQueue
from arb.bookie import BetResult
from arb.bot import ArbBot
from arb.config import Config
from arb.engine import Leg, Opportunity
from arb.web_bookie import PreparedBet, WebBookieAdapter


# -- fakes -------------------------------------------------------------------
class FakeLocator:
    def __init__(self, page, sel):
        self.page, self.sel = page, sel
        self.first = self.last = self

    def click(self, timeout=None):
        self.page.clicks.append(self.sel)

    def fill(self, text):
        self.page.values[self.sel] = text

    def press_sequentially(self, text, delay=0):
        self.page.values[self.sel] = self.page.values.get(self.sel, "") + text

    def press(self, key):
        pass

    def input_value(self, timeout=None):
        return self.page.values.get(self.sel, "")

    def inner_text(self, timeout=None):
        return self.page.body


class FakePage:
    url = "https://sportsbook.draftkings.com/"

    def __init__(self, elements, body=""):
        self.elements, self.body = elements, body
        self.clicks, self.values, self.gotos = [], {}, []

    def evaluate(self, js, arg):
        return self.elements

    def locator(self, sel):
        return FakeLocator(self, sel)

    def title(self):
        return "Sportsbook"

    def wait_for_load_state(self, *_):
        pass

    def wait_for_timeout(self, *_):
        pass

    def goto(self, url, timeout=None):
        self.gotos.append(url)


def _el(ref, text, tag="button"):
    return {"ref": ref, "tag": tag, "role": "", "text": text, "label": "",
            "placeholder": "", "in_view": True}


class ScriptedClient:
    """Returns one canned assistant turn per call; records tool results."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.results = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        last = kw["messages"][-1]
        if last["role"] == "user" and isinstance(last["content"], list):
            self.results.append(last["content"])
        calls = self.turns.pop(0)
        content = [NS(type="tool_use", id=f"t{i}", name=n, input=inp)
                   for i, (n, inp) in enumerate(calls)]
        return NS(stop_reason="tool_use", content=content)


def _agent(page, client, stake=25.0):
    return BetAgent(page, book="draftkings", allowed_domains=["draftkings.com"],
                    stake=stake, client=client)


def _run(agent):
    return agent.run(event="A vs B", home="A", away="B", commence="2026-11-01T00:00Z",
                     outcome="A", target_odds=2.1)


# -- agent guard rails ----------------------------------------------------------
def test_agent_cannot_click_place_bet():
    page = FakePage([_el("1", "Place Bet $25.00")])
    client = ScriptedClient([[("snapshot", {})], [("click", {"ref": "1"})],
                             [("abort", {"reason": "done"})]])
    out = _run(_agent(page, client))
    assert not out.ready
    assert page.clicks == []
    assert "refused" in client.results[-1][0]["content"]


def test_agent_stake_is_engine_sized_not_model_chosen():
    page = FakePage([_el("7", "", tag="input")])
    client = ScriptedClient([
        [("snapshot", {})],
        [("type_text", {"ref": "7", "text": "500", "press_enter": False})],
        [("enter_stake", {"ref": "7"})],
        [("slip_ready", {"selection_shown": "A", "odds_shown": "+110",
                         "stake_shown": "25.00"})],
    ])
    out = _run(_agent(page, client, stake=25.0))
    assert out.ready and out.stake_ref == "7"
    assert page.values['[data-arb-ref="7"]'] == "25.00"
    assert "refused" in client.results[1][0]["content"]


def test_agent_navigation_is_domain_locked():
    page = FakePage([])
    client = ScriptedClient([[("navigate", {"url": "https://evil.example.com/x"})],
                             [("navigate", {"url": "https://sportsbook.draftkings.com/nba"})],
                             [("abort", {"reason": "x"})]])
    _run(_agent(page, client))
    assert page.gotos == ["https://sportsbook.draftkings.com/nba"]


def test_agent_ready_without_stake_is_rejected():
    client = ScriptedClient([[("slip_ready", {"selection_shown": "A", "odds_shown": "2.1",
                                              "stake_shown": "?"})]])
    out = _run(_agent(FakePage([]), client))
    assert not out.ready and "without entering stake" in out.message


# -- slip verification ----------------------------------------------------------
def _web(body, stake_value):
    a = WebBookieAdapter({})
    a.name = "draftkings"
    page = FakePage([], body=body)
    page.values['[data-arb-ref="7"]'] = stake_value
    return a, page


def test_check_slip_passes_and_catches_drift_and_stake():
    a, page = _web("Bet slip  A  +110  Wager", "25.00")
    assert a._check_slip(page, "A", 2.10, 25.0, "7") is None
    assert "price moved" in a._check_slip(page, "A", 2.30, 25.0, "7")
    a, page = _web("Bet slip  A  +110", "250.00")
    assert "stake field" in a._check_slip(page, "A", 2.10, 25.0, "7")


# -- approvals ------------------------------------------------------------------
def test_approval_queue_approve_and_timeout():
    q = ApprovalQueue()
    got = {}
    t = threading.Thread(target=lambda: got.setdefault("v", q.request({"e": 1}, 5)))
    t.start()
    while not q.list():
        time.sleep(0.01)
    assert q.decide(q.list()[0]["id"], True)
    t.join()
    assert got["v"] is True and q.list() == []
    assert q.request({"e": 2}, 0.05) is False


# -- bot two-phase flow ------------------------------------------------------------
class FakeWebAdapter(WebBookieAdapter):
    def __init__(self, name, prepare_ok=True):
        super().__init__({})
        self.name, self.prepare_ok, self.committed = name, prepare_ok, False

    def prepare_bet(self, opp, leg, stake):
        if not self.prepare_ok:
            return BetResult(False, message="event not found")
        return PreparedBet(self, opp, leg, stake, "1", leg.odds)

    def commit(self, prepared):
        self.committed = True
        return BetResult(True, message="ok")


class MemStorage:
    def __init__(self):
        self.bets = []

    def record_opportunity(self, opp):
        pass

    def record_bet(self, opp, leg, status, ref, message):
        self.bets.append((leg.bookie, status))


def _bot(mode, adapters):
    cfg = Config({"dry_run": False, "execution_mode": mode, "approval_timeout_sec": 0.05,
                  "odds_api_key": "x"})
    bot = ArbBot(cfg, MemStorage())
    bot.adapters = {a.name: a for a in adapters}
    bot.balances = {a.name: 1000.0 for a in adapters}
    return bot


def _opp():
    legs = [Leg("A", "draftkings", 2.1, 50.0), Leg("B", "fanduel", 2.1, 50.0)]
    return Opportunity("e1", "nba", "A vs B", "A", "B", "t", legs, 4.0, 100.0, 5.0)


def test_unapproved_arb_places_nothing():
    dk, fd = FakeWebAdapter("draftkings"), FakeWebAdapter("fanduel")
    bot = _bot("approve", [dk, fd])
    bot._handle(_opp())
    assert not dk.committed and not fd.committed
    assert {s for _, s in bot.storage.bets} == {"REJECTED"}


def test_one_failed_slip_aborts_whole_arb():
    dk, fd = FakeWebAdapter("draftkings"), FakeWebAdapter("fanduel", prepare_ok=False)
    bot = _bot("auto", [dk, fd])
    bot._handle(_opp())
    assert not dk.committed and not fd.committed


def test_auto_mode_commits_all_legs():
    dk, fd = FakeWebAdapter("draftkings"), FakeWebAdapter("fanduel")
    bot = _bot("auto", [dk, fd])
    bot._handle(_opp())
    assert dk.committed and fd.committed
    assert [s for _, s in bot.storage.bets] == ["PLACED", "PLACED"]
