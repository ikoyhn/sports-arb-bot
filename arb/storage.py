import json
import sqlite3
import threading
import time


class Storage:
    """Thread-safe SQLite log of detected opportunities and placed/skipped bets."""

    def __init__(self, path="arbbot.db"):
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        with self._lock:
            self.db.execute("""CREATE TABLE IF NOT EXISTS opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, event_id TEXT, sport TEXT, event TEXT,
                commence TEXT, roi_pct REAL, total_stake REAL,
                net_profit REAL, legs_json TEXT)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, event_id TEXT, event TEXT, bookie TEXT, outcome TEXT,
                odds REAL, stake REAL, status TEXT, ref TEXT, message TEXT)""")
            self.db.commit()

    def record_opportunity(self, opp):
        import dataclasses
        with self._lock:
            self.db.execute(
                "INSERT INTO opportunities (ts,event_id,sport,event,commence,"
                "roi_pct,total_stake,net_profit,legs_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (time.time(), opp.event_id, opp.sport, opp.event,
                 opp.commence_time, opp.roi_pct, opp.total_stake,
                 opp.net_profit, json.dumps([dataclasses.asdict(l) for l in opp.legs])))
            self.db.commit()

    def record_bet(self, opp, leg, status, ref, message):
        with self._lock:
            self.db.execute(
                "INSERT INTO bets (ts,event_id,event,bookie,outcome,odds,stake,"
                "status,ref,message) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (time.time(), opp.event_id, opp.event, leg.bookie, leg.outcome,
                 leg.odds, leg.stake, status, ref, message))
            self.db.commit()

    def list_opportunities(self, limit=50):
        with self._lock:
            cur = self.db.execute(
                "SELECT ts,event_id,sport,event,commence,roi_pct,total_stake,"
                "net_profit,legs_json FROM opportunities ORDER BY id DESC LIMIT ?",
                (limit,))
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        out = [dict(zip(cols, r)) for r in rows]
        for o in out:
            o["legs"] = json.loads(o.pop("legs_json"))
        return out

    def list_bets(self, limit=100):
        with self._lock:
            cur = self.db.execute(
                "SELECT ts,event_id,event,bookie,outcome,odds,stake,status,ref,message "
                "FROM bets ORDER BY id DESC LIMIT ?", (limit,))
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        return [dict(zip(cols, r)) for r in rows]
