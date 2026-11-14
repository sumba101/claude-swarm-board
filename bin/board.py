#!/usr/bin/env python3
"""Swarm board: one SQLite file per swarm in ~/.claude/boards/<name>.db.

Agents never write SQL; every write goes through a subcommand here. The
board-inbox hook calls `_inbox` after each tool call to push new posts to the
agent that made it, and the lead watches with `watch` from a Monitor.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sqlite3
import sys
import time
from datetime import date, datetime
from pathlib import Path

BOARDS = Path.home() / ".claude" / "boards"
SEEN = BOARDS / ".seen"
AGENT_TAGS = ("NOTE", "ASK", "ANSWER", "BLOCKED", "DONE")
CATEGORIES = ("security", "performance", "correctness", "design", "dry",
              "simplification", "dead-code", "rule-violation", "other")
SEVERITIES = ("high", "med", "low")
STATUSES = ("VERIFIED", "LIKELY")
# Posts the lead's watch surfaces; NOTE/UNIT/ASSIGN chatter stays on the board.
WATCH_TAGS = ("FOUND", "ASK", "ANSWER", "BLOCKED", "DONE", "RETRACT", "CONFIRM", "CONTEST", "PR")
CI_POLL_S = 60
INBOX_CAP = 4000
NUDGE_EVERY_S = 180
STALE_CLAIM_S = 600
LEAD_OVERDUE_S = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS agents (
  name TEXT PRIMARY KEY, role TEXT NOT NULL DEFAULT 'helper', lane TEXT,
  status TEXT NOT NULL DEFAULT 'working', joined REAL, last_active REAL,
  last_seen_post INTEGER NOT NULL DEFAULT 0, last_nudge REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS units (
  id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES units(id), title TEXT NOT NULL,
  scope TEXT, owner TEXT, state TEXT NOT NULL DEFAULT 'open', verdict TEXT,
  created_by TEXT, updated REAL, brief TEXT, after_id INTEGER, pr_url TEXT, ci TEXT);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY, ts REAL NOT NULL, author TEXT NOT NULL, tag TEXT NOT NULL,
  body TEXT NOT NULL, mentions TEXT NOT NULL DEFAULT ' ', re_post INTEGER,
  re_finding INTEGER);
CREATE TABLE IF NOT EXISTS findings (
  id INTEGER PRIMARY KEY, unit_id INTEGER, author TEXT NOT NULL, title TEXT NOT NULL,
  category TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL,
  evidence TEXT NOT NULL, fix TEXT, effort TEXT, impact TEXT,
  confirmed_by TEXT NOT NULL DEFAULT '', contested_by TEXT NOT NULL DEFAULT '',
  lead_check TEXT, retract_reason TEXT, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS branches (name TEXT PRIMARY KEY);
"""


def ref_id(text: str) -> int:
    """Accept 7, #7 or F7 for unit, post and finding ids."""
    try:
        return int(text.strip().lstrip("#Ff"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an id: {text!r} (use 7, #7 or F7)")


def die(msg: str, code: int = 2) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def db_path(board: str) -> Path:
    if board.endswith(".db") or "/" in board:
        return Path(board).expanduser()
    return BOARDS / f"{board}.db"


def connect(board: str, create: bool = False) -> sqlite3.Connection:
    path = db_path(board)
    if not create and not path.exists():
        die(f"no board at {path} (run: board.sh init <name> \"<goal>\")", 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 15000")
    if not create:
        have = {r["name"] for r in con.execute("PRAGMA table_info(units)")}
        for col, decl in (("brief", "TEXT"), ("after_id", "INTEGER"), ("pr_url", "TEXT"), ("ci", "TEXT")):
            if have and col not in have:
                con.execute(f"ALTER TABLE units ADD COLUMN {col} {decl}")
    return con


def meta(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def lead_name(con: sqlite3.Connection) -> str:
    return meta(con, "lead", "lead")


def touch(con: sqlite3.Connection, agent: str) -> None:
    now = time.time()
    con.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now, agent))
    if agent == lead_name(con):
        con.execute("INSERT OR REPLACE INTO meta VALUES ('lead_last_active', ?)", (str(now),))


def require_agent(con: sqlite3.Connection, agent: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM agents WHERE name = ?", (agent,)).fetchone()
    if row is None:
        die(f"unknown agent '{agent}': run board.sh join <board> {agent} first", 1)
    return row


def mentions_of(body: str) -> str:
    names = set()
    for word in body.replace(",", " ").replace(":", " ").split():
        if word.startswith("@") and len(word) > 1:
            names.add(word[1:].rstrip(".;)"))
    return " " + " ".join(sorted(names)) + " " if names else " "


def add_post(con: sqlite3.Connection, author: str, tag: str, body: str,
             re_post: int | None = None, re_finding: int | None = None) -> int:
    cur = con.execute(
        "INSERT INTO posts (ts, author, tag, body, mentions, re_post, re_finding) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (time.time(), author, tag, body, mentions_of(body), re_post, re_finding))
    touch(con, author)
    return int(cur.lastrowid)


def hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_post(p: sqlite3.Row, cap: int = 0) -> str:
    body = p["body"].replace("\n", " ")
    if cap and len(body) > cap:
        body = body[:cap] + " [...]"
    ref = f" (re #{p['re_post']})" if p["re_post"] else ""
    return f"#{p['id']} [{hhmm(p['ts'])}] {p['author']} {p['tag']}{ref}: {body}"


def open_asks(con: sqlite3.Connection, agent: str | None) -> list[sqlite3.Row]:
    sql = ("SELECT * FROM posts a WHERE a.tag = 'ASK' AND NOT EXISTS ("
           "SELECT 1 FROM posts b WHERE b.tag = 'ANSWER' AND b.re_post = a.id)")
    args: tuple = ()
    if agent:
        sql += " AND a.author != ? AND (a.mentions LIKE ? OR a.mentions LIKE '% all %')"
        args = (agent, f"% {agent} %")
    return con.execute(sql + " ORDER BY a.id", args).fetchall()


def fmt_finding(f: sqlite3.Row) -> str:
    extra = []
    if f["confirmed_by"].strip():
        extra.append(f"confirmed by {f['confirmed_by'].strip()}")
    if f["contested_by"].strip():
        extra.append(f"CONTESTED by {f['contested_by'].strip()}")
    if f["lead_check"]:
        extra.append("lead-checked")
    tail = f" ({'; '.join(extra)})" if extra else ""
    return (f"F{f['id']} [{f['category']}/{f['severity']} {f['status']}] {f['title']} "
            f"| {f['evidence']} | by {f['author']}{tail}")


# ---------------------------------------------------------------- commands

def cmd_init(a: argparse.Namespace) -> None:
    path = db_path(a.board)
    if path.exists():
        die(f"board already exists: {path}", 1)
    con = connect(a.board, create=True)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    now = time.time()
    con.executemany("INSERT INTO meta VALUES (?, ?)", [
        ("goal", a.goal), ("created", str(now)), ("lead", a.lead),
        ("lead_last_active", str(now))])
    con.execute("INSERT INTO agents (name, role, lane, joined, last_active) VALUES (?, 'lead', 'lead', ?, ?)",
                (a.lead, now, now))
    print(path)


def cmd_join(a: argparse.Namespace) -> None:
    con = connect(a.board)
    now = time.time()
    con.execute(
        "INSERT INTO agents (name, role, lane, joined, last_active) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET role = excluded.role, lane = COALESCE(excluded.lane, lane), "
        "status = 'working', last_active = excluded.last_active",
        (a.agent, a.role, a.lane, now, now))
    print(f"joined {db_path(a.board)} as {a.agent} ({a.role}). New posts reach you after each tool call.")
    print(f"Goal: {meta(con, 'goal')}")
    if meta(con, "preflight"):
        print(f"Setup notes from the lead (apply before any install or test run): {meta(con, 'preflight')}")
    cmd_status_print(con)


def cmd_unit(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    if a.parent and not con.execute("SELECT 1 FROM units WHERE id = ?", (a.parent,)).fetchone():
        die(f"no unit #{a.parent}", 1)
    if a.brief and not Path(a.brief).expanduser().exists():
        die(f"brief file not found: {a.brief}", 1)
    cur = con.execute(
        "INSERT INTO units (parent_id, title, scope, created_by, updated, brief, after_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a.parent, a.title, a.scope, a.agent, time.time(), a.brief, a.after))
    uid = int(cur.lastrowid)
    under = f" under #{a.parent}" if a.parent else ""
    add_post(con, a.agent, "UNIT", f"added unit #{uid}{under}: {a.title}" + (f" [{a.scope}]" if a.scope else ""))
    print(uid)


def cmd_claim(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    con.execute("BEGIN IMMEDIATE")
    try:
        if a.unit:
            row = con.execute("SELECT * FROM units WHERE id = ?", (a.unit,)).fetchone()
            if row is None:
                raise SystemExit(f"no unit #{a.unit}")
            if row["state"] != "open" or row["owner"]:
                raise SystemExit(f"unit #{a.unit} is {row['state']} (owner {row['owner']}); pick another or run board.sh claim with no id")
        else:
            row = con.execute(
                "SELECT u.* FROM units u WHERE u.state = 'open' AND u.owner IS NULL AND (u.after_id IS NULL OR "
                "EXISTS (SELECT 1 FROM units d WHERE d.id = u.after_id AND d.state IN ('done', 'dropped'))) "
                "ORDER BY u.id LIMIT 1").fetchone()
            if row is None:
                raise SystemExit("no open units left: post DONE if your own units are finished, or ask @lead for work")
        con.execute("UPDATE units SET owner = ?, state = 'claimed', updated = ? WHERE id = ?",
                    (a.agent, time.time(), row["id"]))
        con.execute("UPDATE agents SET status = 'working' WHERE name = ?", (a.agent,))
        touch(con, a.agent)
        con.execute("COMMIT")
    except SystemExit as e:
        con.execute("ROLLBACK")
        die(str(e), 3)
    print(f"claimed #{row['id']}: {row['title']}" + (f" [{row['scope']}]" if row["scope"] else ""))
    if row["brief"]:
        print(f"brief: {row['brief']} (read it before anything else)")


def set_unit_state(a: argparse.Namespace, state: str, verb: str, note: str | None) -> None:
    con = connect(a.board)
    agent = require_agent(con, a.agent)
    row = con.execute("SELECT * FROM units WHERE id = ?", (a.unit,)).fetchone()
    if row is None:
        die(f"no unit #{a.unit}", 1)
    if row["owner"] != a.agent and agent["role"] not in ("lead", "captain"):
        die(f"unit #{a.unit} is owned by {row['owner']}; only its owner, a captain or the lead can change it", 3)
    owner = None if state == "open" else row["owner"]
    con.execute("UPDATE units SET state = ?, owner = ?, updated = ? WHERE id = ?",
                (state, owner, time.time(), a.unit))
    add_post(con, a.agent, "UNIT", f"{verb} unit #{a.unit} ({row['title']})" + (f": {note}" if note else ""))
    print(f"unit #{a.unit} -> {state}")


def cmd_finish(a: argparse.Namespace) -> None:
    set_unit_state(a, "done", "finished", a.note)


def cmd_release(a: argparse.Namespace) -> None:
    set_unit_state(a, "open", "released", a.note)


def cmd_drop(a: argparse.Namespace) -> None:
    set_unit_state(a, "dropped", "dropped", a.reason)


def cmd_verdict(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    if con.execute("UPDATE units SET verdict = ?, updated = ? WHERE id = ?",
                   (a.text, time.time(), a.unit)).rowcount == 0:
        die(f"no unit #{a.unit}", 1)
    add_post(con, a.agent, "VERDICT", f"unit #{a.unit}: {a.text}")
    print(f"verdict recorded on #{a.unit}")


def cmd_assign(a: argparse.Namespace) -> None:
    con = connect(a.board)
    agent = require_agent(con, a.agent)
    if agent["role"] not in ("lead", "captain"):
        die("only the lead or a captain can assign", 3)
    require_agent(con, a.to)
    row = con.execute("SELECT * FROM units WHERE id = ?", (a.unit,)).fetchone()
    if row is None:
        die(f"no unit #{a.unit}", 1)
    prev = row["owner"]
    con.execute("UPDATE units SET owner = ?, state = 'claimed', updated = ? WHERE id = ?",
                (a.to, time.time(), a.unit))
    note = f" (taken from @{prev})" if prev and prev != a.to else ""
    add_post(con, a.agent, "ASSIGN", f"@{a.to} you now own unit #{a.unit}: {row['title']}{note}"
             + (f". {a.note}" if a.note else ""))
    print(f"unit #{a.unit} -> {a.to}")


def cmd_pr(a: argparse.Namespace) -> None:
    con = connect(a.board)
    agent = require_agent(con, a.agent)
    row = con.execute("SELECT * FROM units WHERE id = ?", (a.unit,)).fetchone()
    if row is None:
        die(f"no unit #{a.unit}", 1)
    if row["owner"] != a.agent and agent["role"] not in ("lead", "captain"):
        die(f"unit #{a.unit} is owned by {row['owner']}", 3)
    con.execute("UPDATE units SET pr_url = ?, ci = 'pending', updated = ? WHERE id = ?", (a.url, time.time(), a.unit))
    add_post(con, a.agent, "PR", f"unit #{a.unit} ({row['title']}): {a.url}")
    print(f"unit #{a.unit} PR recorded; the lead's watch now tracks its CI")


def cmd_retire(a: argparse.Namespace) -> None:
    con = connect(a.board)
    if a.agent != lead_name(con):
        die("only the lead retires agents", 3)
    held = con.execute("SELECT id FROM units WHERE owner = ? AND state = 'claimed'", (a.who,)).fetchall()
    if held:
        die(f"{a.who} still holds {', '.join('#' + str(u['id']) for u in held)}: finish, release or drop them first", 3)
    if con.execute("UPDATE agents SET status = 'done' WHERE name = ?", (a.who,)).rowcount == 0:
        die(f"unknown agent {a.who}", 1)
    add_post(con, a.agent, "NOTE", f"retired {a.who}" + (f": {a.note}" if a.note else ""))
    print(f"{a.who} retired")


def cmd_preflight(a: argparse.Namespace) -> None:
    con = connect(a.board)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('preflight', ?)", (a.text,))
    add_post(con, lead_name(con), "NOTE", f"@all setup notes updated: {a.text}")
    print("preflight saved; every join prints it")


def cmd_post(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    tag = a.tag.upper()
    if tag not in AGENT_TAGS:
        die(f"bad tag {tag}: use one of {', '.join(AGENT_TAGS)} (findings go through board.sh find)", 2)
    if tag == "ANSWER" and not a.re:
        die("ANSWER needs --re <post id of the ASK>", 2)
    if tag == "ASK":
        gone = [r["name"] for r in con.execute("SELECT name FROM agents WHERE status = 'done'")
                if f" {r['name']} " in mentions_of(a.message)]
        if gone and a.agent != lead_name(con):
            die(f"{', '.join(gone)} already posted DONE and will not see this. Answer it yourself from the "
                f"code, or ask @{lead_name(con)}, who can resume a finished agent.", 3)
        if gone:
            print(f"warning: {', '.join(gone)} is done and will not see this; resume it with SendMessage", file=sys.stderr)
    if tag == "DONE":
        pending = open_asks(con, a.agent)
        held = con.execute("SELECT id, title FROM units WHERE owner = ? AND state = 'claimed'", (a.agent,)).fetchall()
        if pending or held:
            lines = [fmt_post(p, 200) for p in pending] + [f"unit #{u['id']} still claimed: {u['title']}" for u in held]
            die("refusing DONE until these are handled (ANSWER --re, or finish/release your units):\n" + "\n".join(lines), 3)
        con.execute("UPDATE agents SET status = 'done' WHERE name = ?", (a.agent,))
    pid = add_post(con, a.agent, tag, a.message, re_post=a.re)
    print(f"#{pid}")


def read_json_stdin() -> dict:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        die(f"stdin must be one JSON object: {e}", 2)
    if not isinstance(data, dict):
        die("stdin must be one JSON object", 2)
    return data


def validate_finding(d: dict, partial: bool) -> dict:
    allowed = {"title", "category", "severity", "status", "evidence", "fix", "effort", "impact", "unit"}
    unknown = set(d) - allowed
    if unknown:
        die(f"unknown fields {sorted(unknown)}; allowed {sorted(allowed)}", 2)
    if not partial:
        missing = [k for k in ("title", "category", "severity", "status", "evidence") if not d.get(k)]
        if missing:
            die(f"missing fields {missing}", 2)
    checks = (("category", CATEGORIES), ("severity", SEVERITIES), ("status", STATUSES),
              ("effort", ("S", "M", "L", "S-M", "M-L")), ("impact", SEVERITIES))
    for key, ok in checks:
        if key in d and d[key] is not None and d[key] not in ok:
            die(f"{key} must be one of {ok}", 2)
    if isinstance(d.get("evidence"), list):
        d["evidence"] = "; ".join(d["evidence"])
    return d


def cmd_find(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    d = validate_finding(read_json_stdin(), partial=False)
    now = time.time()
    cur = con.execute(
        "INSERT INTO findings (unit_id, author, title, category, severity, status, evidence, fix, effort, impact, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (d.get("unit"), a.agent, d["title"], d["category"], d["severity"], d["status"],
         d["evidence"], d.get("fix"), d.get("effort"), d.get("impact"), now, now))
    fid = int(cur.lastrowid)
    f = con.execute("SELECT * FROM findings WHERE id = ?", (fid,)).fetchone()
    add_post(con, a.agent, "FOUND", fmt_finding(f), re_finding=fid)
    print(f"F{fid}")


def cmd_amend(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    f = con.execute("SELECT * FROM findings WHERE id = ?", (a.fid,)).fetchone()
    if f is None:
        die(f"no finding F{a.fid}", 1)
    if f["author"] != a.agent and a.agent != lead_name(con):
        die(f"F{a.fid} belongs to {f['author']}; contest it instead", 3)
    d = validate_finding(read_json_stdin(), partial=True)
    if "unit" in d:
        d["unit_id"] = d.pop("unit")
    if not d:
        die("nothing to amend", 2)
    cols = ", ".join(f"{k} = ?" for k in d)
    con.execute(f"UPDATE findings SET {cols}, updated = ? WHERE id = ?", (*d.values(), time.time(), a.fid))
    add_post(con, a.agent, "AMEND", f"F{a.fid} amended: {', '.join(sorted(d))}", re_finding=a.fid)
    print(f"F{a.fid} amended")


def cmd_vote(a: argparse.Namespace, column: str, tag: str) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    f = con.execute("SELECT * FROM findings WHERE id = ?", (a.fid,)).fetchone()
    if f is None:
        die(f"no finding F{a.fid}", 1)
    if f["author"] == a.agent:
        die("you cannot confirm or contest your own finding", 3)
    if f" {a.agent} " not in f" {f[column]} ":
        con.execute(f"UPDATE findings SET {column} = trim({column} || ' ' || ?), updated = ? WHERE id = ?",
                    (a.agent, time.time(), a.fid))
    add_post(con, a.agent, tag, f"@{f['author']} F{a.fid} ({f['title']}): {a.evidence}", re_finding=a.fid)
    print(f"{tag.lower()}ed F{a.fid}")


def cmd_retract(a: argparse.Namespace) -> None:
    con = connect(a.board)
    require_agent(con, a.agent)
    f = con.execute("SELECT * FROM findings WHERE id = ?", (a.fid,)).fetchone()
    if f is None:
        die(f"no finding F{a.fid}", 1)
    if f["author"] != a.agent and a.agent != lead_name(con):
        die(f"F{a.fid} belongs to {f['author']}; contest it instead", 3)
    con.execute("UPDATE findings SET status = 'RETRACTED', retract_reason = ?, updated = ? WHERE id = ?",
                (a.reason, time.time(), a.fid))
    add_post(con, a.agent, "RETRACT", f"F{a.fid} ({f['title']}): {a.reason}", re_finding=a.fid)
    print(f"F{a.fid} retracted")


def cmd_check(a: argparse.Namespace) -> None:
    con = connect(a.board)
    if a.agent != lead_name(con):
        die("only the lead records checks", 3)
    if con.execute("UPDATE findings SET lead_check = ?, updated = ? WHERE id = ?",
                   (a.note, time.time(), a.fid)).rowcount == 0:
        die(f"no finding F{a.fid}", 1)
    add_post(con, a.agent, "CHECK", f"F{a.fid}: {a.note}", re_finding=a.fid)
    print(f"F{a.fid} checked")


def cmd_read(a: argparse.Namespace) -> None:
    con = connect(a.board)
    sql, args = "SELECT * FROM posts WHERE id > ?", [a.since]
    if a.agent:
        sql += " AND (mentions LIKE ? OR mentions LIKE '% all %' OR author = ?)"
        args += [f"% {a.agent} %", a.agent]
    if a.tag:
        sql += " AND tag = ?"
        args.append(a.tag.upper())
    for p in con.execute(sql + " ORDER BY id", args):
        print(fmt_post(p))


def cmd_status_print(con: sqlite3.Connection) -> None:
    units = con.execute("SELECT state, count(*) n FROM units GROUP BY state").fetchall()
    print("units: " + (", ".join(f"{u['state']} {u['n']}" for u in units) or "none yet"))
    for ag in con.execute("SELECT * FROM agents ORDER BY role, name"):
        held = con.execute("SELECT group_concat('#' || id, ' ') h FROM units WHERE owner = ? AND state = 'claimed'",
                           (ag["name"],)).fetchone()["h"] or "-"
        idle = int(time.time() - (ag["last_active"] or 0))
        print(f"  {ag['name']:<14} {ag['role']:<8} {ag['status']:<8} holds {held:<12} last active {idle}s ago"
              + (f"  lane: {ag['lane']}" if ag["lane"] and ag["role"] != "lead" else ""))
    fs = con.execute("SELECT status, count(*) n FROM findings GROUP BY status").fetchall()
    print("findings: " + (", ".join(f"{f['status']} {f['n']}" for f in fs) or "none yet"))
    asks = open_asks(con, None)
    if asks:
        print(f"open asks: {len(asks)}")
        for p in asks:
            print("  " + fmt_post(p, 200))


def cmd_status(a: argparse.Namespace) -> None:
    con = connect(a.board)
    print(f"goal: {meta(con, 'goal')}" + ("  [CLOSED]" if meta(con, "closed") else ""))
    cmd_status_print(con)
    open_units = con.execute("SELECT * FROM units WHERE state IN ('open', 'claimed') ORDER BY id").fetchall()
    for u in open_units:
        parent = f" <#{u['parent_id']}" if u["parent_id"] else ""
        verdict = f" | verdict: {u['verdict']}" if u["verdict"] else ""
        print(f"  #{u['id']}{parent} {u['state']:<7} {u['owner'] or '-':<12} {u['title']}{verdict}")


def cmd_units(a: argparse.Namespace) -> None:
    con = connect(a.board)
    for u in con.execute("SELECT * FROM units ORDER BY id"):
        parent = f" <#{u['parent_id']}" if u["parent_id"] else ""
        verdict = f" | verdict: {u['verdict']}" if u["verdict"] else ""
        scope = f" [{u['scope']}]" if u["scope"] else ""
        extra = "".join([
            f" after #{u['after_id']}" if u["after_id"] else "",
            " +brief" if u["brief"] else "",
            f" PR {u['pr_url'].rsplit('/', 1)[-1]} ci:{u['ci']}" if u["pr_url"] else "",
        ])
        print(f"#{u['id']}{parent} {u['state']:<7} {u['owner'] or '-':<12} {u['title']}{scope}{extra}{verdict}")


SEV_ORDER = "CASE severity WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END"


def cmd_findings(a: argparse.Namespace) -> None:
    con = connect(a.board)
    where = "" if a.all else "WHERE status != 'RETRACTED'"
    rows = con.execute(f"SELECT * FROM findings {where} ORDER BY {SEV_ORDER}, category, id").fetchall()
    if a.json:
        print(json.dumps([dict(r) for r in rows], indent=1))
        return
    if a.md:
        for r in rows:
            print(f"### F{r['id']}. {r['title']}\n")
            print(f"- **{r['category']} / {r['severity']} / {r['status']}**, effort {r['effort'] or '?'}, impact {r['impact'] or '?'}, by {r['author']}")
            print(f"- Evidence: {r['evidence']}")
            if r["fix"]:
                print(f"- Fix: {r['fix']}")
            if r["confirmed_by"].strip() or r["contested_by"].strip():
                print(f"- Confirmed by: {r['confirmed_by'] or '-'}; contested by: {r['contested_by'] or '-'}")
            if r["lead_check"]:
                print(f"- Lead check: {r['lead_check']}")
            if r["retract_reason"]:
                print(f"- Retracted: {r['retract_reason']}")
            print()
        return
    for r in rows:
        print(fmt_finding(r))


def cmd_open_asks(a: argparse.Namespace) -> None:
    con = connect(a.board)
    for p in open_asks(con, a.agent):
        print(fmt_post(p))


def cmd_sql(a: argparse.Namespace) -> None:
    path = db_path(a.board)
    if not path.exists():
        die(f"no board at {path}", 1)
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(a.query).fetchall()
    except sqlite3.Error as e:
        die(f"sql error: {e} (read-only; use the write subcommands)", 2)
    if rows:
        print("\t".join(rows[0].keys()))
    for r in rows:
        print("\t".join("" if v is None else str(v) for v in r))


def cmd_link(a: argparse.Namespace) -> None:
    con = connect(a.board)
    con.executemany("INSERT OR IGNORE INTO branches VALUES (?)", [(b,) for b in a.branches])
    print(f"linked {len(a.branches)} branch(es)")


def cmd_branches(a: argparse.Namespace) -> None:
    con = connect(a.board)
    for r in con.execute("SELECT name FROM branches ORDER BY name"):
        print(r["name"])


def cmd_close(a: argparse.Namespace) -> None:
    con = connect(a.board)
    con.execute("INSERT OR REPLACE INTO meta VALUES ('closed', ?)", (str(time.time()),))
    add_post(con, lead_name(con), "NOTE", "@all board closed by the lead; stop work and post DONE")
    print("closed")


def drop_seen(path: Path) -> None:
    if not SEEN.is_dir():
        return
    for state in SEEN.iterdir():
        try:
            if state.read_text().split("\t", 1)[0] == str(path):
                state.unlink()
        except OSError:
            pass


def cmd_archive(a: argparse.Namespace) -> None:
    path = db_path(a.board)
    if not path.exists():
        die(f"no board at {path}", 1)
    dest = BOARDS / "archive" / f"{path.stem}-{date.today().isoformat()}.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    con = connect(a.board)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    shutil.move(str(path), str(dest))
    for side in ("-wal", "-shm"):
        Path(str(path) + side).unlink(missing_ok=True)
    drop_seen(path)
    print(f"archived to {dest}")


# ---------------------------------------------------------------- lead watch

def pr_ci_state(url: str) -> str:
    try:
        out = subprocess.run(["gh", "pr", "checks", url, "--json", "bucket", "--jq", "[.[].bucket] | join(\" \")"],
                             capture_output=True, text=True, timeout=30).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if not out or "pending" in out:
        return "pending"
    return "red" if "fail" in out or "cancel" in out else "green"


def cmd_watch(a: argparse.Namespace) -> None:
    con = connect(a.board)
    lead = lead_name(con)
    last = con.execute("SELECT coalesce(max(id), 0) m FROM posts").fetchone()["m"]
    last_digest = time.time()
    raised: dict[str, float] = {}  # alert key -> first seen; re-emitted as LEAD OVERDUE once
    last_ci = 0.0

    def emit(line: str) -> None:
        print(line, flush=True)

    while True:
        if meta(con, "closed"):
            emit("BOARD CLOSED: watch ends")
            return
        now = time.time()
        for p in con.execute("SELECT * FROM posts WHERE id > ? ORDER BY id", (last,)).fetchall():
            last = p["id"]
            to_lead = f" {lead} " in p["mentions"]
            if p["author"] != lead and (p["tag"] in WATCH_TAGS or to_lead):
                emit(("FOR LEAD " if to_lead else "") + fmt_post(p, 500))

        alerts: dict[str, str] = {}
        for p in open_asks(con, lead):
            alerts[f"ask{p['id']}"] = f"UNANSWERED ASK for you: {fmt_post(p, 300)}"
        for p in con.execute("SELECT * FROM posts b WHERE tag = 'BLOCKED' AND NOT EXISTS ("
                             "SELECT 1 FROM posts r WHERE r.author = ? AND r.id > b.id)", (lead,)):
            alerts[f"blocked{p['id']}"] = f"BLOCKED agent waiting on you: {fmt_post(p, 300)}"
        open_n = con.execute("SELECT count(*) n FROM units WHERE state = 'open'").fetchone()["n"]
        for ag in con.execute("SELECT * FROM agents WHERE role != 'lead' AND status = 'working'"):
            held = con.execute("SELECT count(*) n FROM units WHERE owner = ? AND state = 'claimed'",
                               (ag["name"],)).fetchone()["n"]
            if held == 0 and open_n == 0 and now - (ag["joined"] or now) > 120:
                alerts[f"idle-{ag['name']}"] = (f"IDLE: {ag['name']} holds no unit and none are open; "
                                                 f"split a big unit, assign it work, or tell it to post DONE")
            if held and now - (ag["last_active"] or now) > STALE_CLAIM_S:
                alerts[f"stale-{ag['name']}"] = (f"STALE: {ag['name']} holds {held} unit(s) but has been silent "
                                                  f"{int(now - ag['last_active'])}s; check on it or reassign")
        for p in open_asks(con, None):
            stranded = [r["name"] for r in con.execute("SELECT name FROM agents WHERE status = 'done'")
                        if f" {r['name']} " in p["mentions"]]
            if stranded:
                alerts[f"stranded{p['id']}"] = (f"STRANDED ASK to finished agent {', '.join(stranded)}: "
                                                 f"{fmt_post(p, 300)}. Answer it yourself or resume the agent")
        agents_left = con.execute("SELECT count(*) n FROM agents WHERE role != 'lead' AND status != 'done'").fetchone()["n"]
        joined = con.execute("SELECT count(*) n FROM agents WHERE role != 'lead'").fetchone()["n"]
        if joined and agents_left == 0:
            unchecked = con.execute("SELECT count(*) n FROM findings WHERE status != 'RETRACTED' "
                                    "AND severity IN ('high', 'med') AND lead_check IS NULL").fetchone()["n"]
            asks_left = len(open_asks(con, None))
            alerts["alldone"] = (f"ALL AGENTS DONE: {unchecked} high/med finding(s) not lead-checked, "
                                 f"{asks_left} ASK(s) unanswered. Resolve those, then board.sh findings --md "
                                 f"and close the board")

        for key, text in alerts.items():
            if key not in raised:
                raised[key] = now
                emit(text)
        lead_active = float(meta(con, "lead_last_active", "0"))
        for key in list(raised):
            if key not in alerts:
                del raised[key]
            elif raised[key] > 0 and now - raised[key] > LEAD_OVERDUE_S and lead_active < raised[key]:
                emit(f"LEAD OVERDUE ({int(now - raised[key])}s, no lead action since): {alerts[key]}")
                raised[key] = -1  # escalate once per alert

        if now - last_ci >= CI_POLL_S:
            last_ci = now
            for u in con.execute("SELECT * FROM units WHERE pr_url IS NOT NULL AND ci = 'pending'").fetchall():
                state = pr_ci_state(u["pr_url"])
                if state in ("green", "red"):
                    con.execute("UPDATE units SET ci = ? WHERE id = ?", (state, u["id"]))
                    emit(f"CI {state.upper()}: unit #{u['id']} {u['pr_url']}"
                         + (" (owner may have left: finish the unit if it is done)" if state == "green" else
                            " (tell the owner or fix it yourself)"))

        if now - last_digest >= a.digest:
            last_digest = now
            states = dict(con.execute("SELECT state, count(*) FROM units GROUP BY state").fetchall())
            fs = dict(con.execute("SELECT status, count(*) FROM findings GROUP BY status").fetchall())
            emit(f"DIGEST: units open {states.get('open', 0)} claimed {states.get('claimed', 0)} "
                 f"done {states.get('done', 0)} | agents working {agents_left}/{joined} | findings "
                 f"VERIFIED {fs.get('VERIFIED', 0)} LIKELY {fs.get('LIKELY', 0)} RETRACTED {fs.get('RETRACTED', 0)} "
                 f"| open alerts {len(alerts)}. Review with board.sh status")
        time.sleep(a.interval)


# ---------------------------------------------------------------- inbox hook

def cmd_inbox(a: argparse.Namespace) -> None:
    path = Path(a.db)
    if not path.exists():
        return
    con = connect(str(path))
    ag = con.execute("SELECT * FROM agents WHERE name = ?", (a.agent,)).fetchone()
    if ag is None or ag["status"] == "done":
        return
    now = time.time()
    con.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now, a.agent))
    if a.mark_read:
        con.execute("UPDATE agents SET last_seen_post = (SELECT coalesce(max(id), 0) FROM posts) WHERE name = ?",
                    (a.agent,))
        return
    posts = con.execute("SELECT * FROM posts WHERE id > ? AND author != ? AND tag != 'UNIT' ORDER BY id",
                        (ag["last_seen_post"], a.agent)).fetchall()
    top = con.execute("SELECT coalesce(max(id), 0) m FROM posts").fetchone()["m"]
    con.execute("UPDATE agents SET last_seen_post = ? WHERE name = ?", (top, a.agent))

    lines = []
    mine = 0
    for p in posts:
        to_me = f" {a.agent} " in p["mentions"] or " all " in p["mentions"]
        mine += to_me
        lines.append(("FOR YOU " if to_me else "") + fmt_post(p, 400))
    nudge = ""
    held = con.execute("SELECT count(*) n FROM units WHERE owner = ? AND state = 'claimed'", (a.agent,)).fetchone()["n"]
    if held == 0 and now - ag["last_nudge"] > NUDGE_EVERY_S and now - (ag["joined"] or now) > 60:
        open_n = con.execute("SELECT count(*) n FROM units WHERE state = 'open'").fetchone()["n"]
        board = path.stem
        nudge = (f"You hold no unit. {open_n} open: run board.sh claim {board} {a.agent}" if open_n else
                 f"You hold no unit and none are open: split one with board.sh unit --parent, ask @lead, or post DONE")
        con.execute("UPDATE agents SET last_nudge = ? WHERE name = ?", (now, a.agent))
    if not lines and not nudge:
        return
    head = f"Swarm board {path.stem} update for {a.agent}."
    if mine:
        head += f" {mine} post(s) address you: handle them before continuing (ASK -> board.sh post ... ANSWER --re <id>)."
    msg = "\n".join([head, *lines, *([nudge] if nudge else [])])
    if len(msg) > INBOX_CAP:
        msg = msg[:INBOX_CAP] + f"\n[...] more: board.sh read {path.stem} --since {ag['last_seen_post']}"
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}))


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="board.sh", description="Swarm board on SQLite (one .db per swarm in ~/.claude/boards)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, *args: tuple, help: str = "") -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help)
        sp.add_argument("board")
        for spec in args:
            sp.add_argument(*spec[0], **spec[1])
        sp.set_defaults(fn=fn)
        return sp

    A = lambda *names, **kw: (names, kw)  # noqa: E731
    agent = A("agent")
    add("init", cmd_init, A("goal"), A("--lead", default="lead"), help="create a board; the caller is the lead")
    add("join", cmd_join, agent, A("--role", default="helper", choices=("helper", "captain")), A("--lane"), help="register an agent (the inbox hook starts here)")
    add("unit", cmd_unit, agent, A("title"), A("--scope"), A("--parent", type=ref_id), A("--brief"), A("--after", type=ref_id),
        help="add a work unit; --brief <file> attaches its spec, --after <id> holds it until that unit is done")
    add("pr", cmd_pr, agent, A("unit", type=ref_id), A("url"), help="record the PR for your unit; the lead's watch tracks its CI")
    add("retire", cmd_retire, agent, A("who"), A("note", nargs="?"), help="lead: mark an agent that handed back as done")
    add("preflight", cmd_preflight, A("text"), help="lead: setup notes every join prints (tool versions, env)")
    add("claim", cmd_claim, agent, A("unit", nargs="?", type=ref_id), help="claim a unit atomically (no id = next open)")
    add("finish", cmd_finish, agent, A("unit", type=ref_id), A("note", nargs="?"), help="mark your unit done")
    add("release", cmd_release, agent, A("unit", type=ref_id), A("note", nargs="?"), help="give a unit back to the pool")
    add("drop", cmd_drop, agent, A("unit", type=ref_id), A("reason"), help="drop a unit as not worth doing")
    add("verdict", cmd_verdict, agent, A("unit", type=ref_id), A("text"), help="record a scout verdict on a unit")
    add("assign", cmd_assign, agent, A("unit", type=ref_id), A("to"), A("note", nargs="?"), help="lead/captain: give a unit to an agent")
    add("post", cmd_post, agent, A("tag"), A("message"), A("--re", type=ref_id), help="NOTE|ASK|ANSWER|BLOCKED|DONE; @name addresses")
    add("find", cmd_find, agent, help="record a finding from JSON on stdin; prints F<id>")
    add("amend", cmd_amend, agent, A("fid", type=ref_id), help="update your finding from JSON on stdin")
    add("confirm", lambda a: cmd_vote(a, "confirmed_by", "CONFIRM"), agent, A("fid", type=ref_id), A("evidence"), help="confirm another agent's finding with evidence")
    add("contest", lambda a: cmd_vote(a, "contested_by", "CONTEST"), agent, A("fid", type=ref_id), A("evidence"), help="contest another agent's finding with evidence")
    add("retract", cmd_retract, agent, A("fid", type=ref_id), A("reason"), help="retract your finding")
    add("check", cmd_check, agent, A("fid", type=ref_id), A("note"), help="lead: record your own verification of a finding")
    add("read", cmd_read, A("--since", type=ref_id, default=0), A("--agent"), A("--tag"), help="print posts")
    add("status", cmd_status, help="units, agents, findings, open asks")
    add("units", cmd_units, help="every unit with owner and verdict")
    add("findings", cmd_findings, A("--all", action="store_true"), A("--json", action="store_true"), A("--md", action="store_true"), help="findings by severity")
    add("open-asks", cmd_open_asks, A("agent", nargs="?"), help="ASKs with no ANSWER")
    add("sql", cmd_sql, A("query"), help="read-only SQL")
    add("link", cmd_link, A("branches", nargs="+"), help="record branches this swarm shipped")
    add("branches", cmd_branches, help="list linked branches")
    add("watch", cmd_watch, A("--digest", type=ref_id, default=300), A("--interval", type=float, default=3.0), help="lead's Monitor stream")
    add("close", cmd_close, help="close the board; watch exits")
    add("archive", cmd_archive, help="move the .db to boards/archive")
    ib = sub.add_parser("_inbox")
    ib.add_argument("db")
    ib.add_argument("agent")
    ib.add_argument("--mark-read", action="store_true")
    ib.set_defaults(fn=cmd_inbox)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
