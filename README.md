# Swarm Board for Claude Code

A shared SQLite board that lets a Claude Code lead agent run a swarm of helper
subagents. Helpers claim work units, record structured findings, and talk
through posts. A hook pushes every new post to every helper after each tool
call, and the lead watches a filtered stream with alerts.

It ships as a Claude Code skill (the lead's playbook), a small CLI
(`board.sh`, stdlib Python and SQLite), and one PostToolUse hook.

## Why

Swarms help on search problems such as research and production debugging,
where a single agent tends to stop at the first plausible answer. They work
when the agents share coordination state.

Claude Code's built-in `SendMessage` is point to point:

- The sender must know each recipient's name and address it explicitly.
- Telling everyone means one message per recipient.
- There is no shared view of who is doing what or what has been found.

Sharing one finding with nine peers takes nine tool calls, and in practice the
lead ends up relaying findings by hand. The board replaces that with one shared
SQLite file per swarm. An agent posts once and every agent sees it.

## How it works

**Who is in a swarm.** One lead and N helpers. The lead plans, unblocks and
verifies; the helpers do the work.

**Units.** The lead splits the problem into units, each with a short brief:
what is in scope and what would make it unsafe. It seeds more units than
agents, so there is always something left to pick up. A unit can depend on
another (`--after 3`) and cannot be claimed until that one is done. A helper
whose area is too big becomes a captain and splits its unit into child units.

**Atomic claims.** `claim` gives a helper the lowest-numbered open unit whose
dependencies are done. Claims run in a SQLite transaction, so two helpers can
never hold the same unit.

**Findings.** Results are structured records: title, category, severity,
VERIFIED or LIKELY, evidence, fix. Other agents confirm or contest a finding
with their own evidence. Authors amend or retract their own. The lead records
its own check on every important finding before it reaches you.

**Posts pushed to every agent.** After every tool call, the hook injects all
new posts into the helper's context, except its own posts and claim
bookkeeping. Posts that mention it (`@name`) or everyone (`@all`) are flagged
`FOR YOU`, with a note to answer them first. Each delivery is capped at about
4,000 characters; past that the agent is pointed to `board.sh read`. Nobody
polls and nobody has to remember to check.

Every agent gets every post, addressed or not, because agents rarely address
each other. Cross-checking happens when an agent sees a finding
it was not asked about and confirms or contests it.

**Idle nudges.** If a helper holds no unit and joined more than a minute ago,
the hook appends a nudge: "You hold no unit. 3 open: run board.sh claim". If
nothing is open, it suggests splitting a unit, asking the lead, or posting
DONE. The nudge repeats at most every 3 minutes.

**No loose ends.** An agent cannot post DONE while it holds a unit or owes an
answer to a question aimed at it. A question to an agent that already finished
is refused, because it would never be read.

**The lead's watch.** The lead does not get the full broadcast. It runs
`board.sh watch` under Claude Code's `Monitor` tool, which turns each printed
line into a notification. The watch prints only what needs the lead: findings,
questions, answers, blocked agents, DONE, confirms, contests, retractions, PR
posts, and anything mentioning `@lead`. Every 5 minutes it prints a digest of
units, agents and findings.

**Alerts.** Only the lead's watch raises alerts. Helpers signal with ordinary
posts; a BLOCKED post or a question to `@lead` becomes an alert. The rest are
computed from board state:

- IDLE: an agent holds no unit and none are open.
- STALE: an agent holds a unit but has been silent for 10 minutes.
- STRANDED: a question went to an agent that already finished.
- ALL AGENTS DONE: lists findings the lead has not checked and questions still open.
- CI GREEN / CI RED: a unit's recorded PR finished its checks (polled with `gh`).

An alert the lead leaves alone for 5 minutes, with no lead write to the board
in that time, fires again as `LEAD OVERDUE`.

### Design choices

| Choice | Why |
| --- | --- |
| One shared SQLite file, written only through CLI commands | One source of truth every agent can query; the CLI enforces the rules and safe concurrent writes |
| Atomic claims on a pool of more units than agents | Two agents never take the same unit, and fast agents keep pulling work |
| Structured findings that others confirm or contest | Results merge cleanly, and a wrong guess gets challenged with evidence |
| New posts pushed to every agent after each tool call | A finding in one area reaches every other area at once, with no addressing or polling |
| No finishing while holding work or owing an answer | No stranded units or unanswered questions |
| The lead watches a filtered stream with alerts | The lead handles exceptions, so it scales with the swarm |

## Requirements

- Claude Code
- `python3` (3.8 or later; uses only the standard library, including `sqlite3`)
- `jq` (the hook parses its input with it, and `install.sh` edits settings with it)
- `bash`
- `gh`, only if you want the lead's watch to poll PR checks

## Install

### Option 1: install script

```bash
git clone https://github.com/sumba101/claude-swarm-board.git
cd claude-swarm-board
./install.sh
```

The script:

- copies the skill to `~/.claude/skills/swarm-board/`
- copies `board.py` and `board.sh` to `~/.claude/bin/`
- copies `board-inbox.sh` to `~/.claude/hooks/`
- backs up `~/.claude/settings.json` to `settings.json.bak.<timestamp>`, then
  adds this PostToolUse hook with `jq`, keeping your existing hooks:

```json
{
  "matcher": "*",
  "hooks": [
    { "type": "command", "command": "~/.claude/hooks/board-inbox.sh", "timeout": 5 }
  ]
}
```

Running it again updates the files and leaves the hook alone if it is already
there. Restart Claude Code afterward. Add `~/.claude/bin` to your `PATH` if you
want to type `board.sh` instead of `~/.claude/bin/board.sh`.

### Option 2: Claude Code plugin

The repo is also a plugin and a single-plugin marketplace. In Claude Code:

```text
/plugin marketplace add sumba101/claude-swarm-board
/plugin install swarm-board@swarm-board
```

The plugin registers the hook from `hooks/hooks.json`, puts `board.sh` on the
Bash tool's `PATH` through its `bin/` directory, and loads the skill as
`swarm-board:swarm-board`. Pick one install method; with both, the hook runs
twice per tool call.

### Uninstall

For the install script:

1. Remove the files:
   ```bash
   rm -rf ~/.claude/skills/swarm-board
   rm -f ~/.claude/bin/board.py ~/.claude/bin/board.sh ~/.claude/hooks/board-inbox.sh
   ```
2. Remove the hook entry from `~/.claude/settings.json`:
   ```bash
   jq '.hooks.PostToolUse |= map(select(([.hooks[]?.command] | any(test("board-inbox\\.sh"))) | not))' \
     ~/.claude/settings.json > /tmp/settings.json && mv /tmp/settings.json ~/.claude/settings.json
   ```
3. Optionally delete old boards: `rm -rf ~/.claude/boards`.

For the plugin: `/plugin uninstall swarm-board@swarm-board`.

## Quick start

Ask your main Claude Code session to lead a swarm. For example:

```text
Use the swarm-board skill. Investigate why checkout latency doubled since
Tuesday. Make a board called checkout-latency, seed units for the API logs,
the database slow-query log, the last 20 deploys, and the cache hit rate, and
fan out 4 Sonnet helpers. Each helper takes at most 2 units. Verify every
high or med finding yourself, then give me one report with the evidence.
```

The lead then:

1. runs `board.sh init checkout-latency "<goal>"` and seeds units with briefs,
2. spawns helpers with the agent protocol from the skill pasted into each prompt,
3. starts `Monitor` on `board.sh watch checkout-latency` and acts on alerts and digests,
4. checks findings, prints them with `board.sh findings checkout-latency --md`,
   and closes the board.

Each helper's first command is `board.sh join <board> <name>`. That command is
what the hook keys on: from then on, the helper gets new posts after every tool
call.

Boards live in `~/.claude/boards/<name>.db`. You can also pass a path ending in
`.db` anywhere a board name is expected.

## Command reference

Every command takes the board name first. Commands that act as an agent take
the agent's name second, for example `board.sh claim <board> <agent> [unit]`.

| Command | What it does |
| --- | --- |
| `init <board> "<goal>" [--lead NAME]` | create a board; the caller is the lead |
| `join <board> <agent> [--role helper\|captain] [--lane L]` | register an agent (the inbox hook starts here) |
| `unit <board> <agent> "<title>" [--scope S] [--parent ID] [--brief FILE] [--after ID]` | add a work unit; `--brief` attaches its spec, `--after` holds it until that unit is done |
| `claim <board> <agent> [unit]` | claim a unit atomically (no id = next open) |
| `verdict <board> <agent> <unit> "<text>"` | record a scout verdict on a unit |
| `finish <board> <agent> <unit> [note]` | mark your unit done |
| `release <board> <agent> <unit> [note]` | give a unit back to the pool |
| `drop <board> <agent> <unit> "<reason>"` | drop a unit as not worth doing |
| `assign <board> <agent> <unit> <to> [note]` | lead/captain: give a unit to an agent |
| `pr <board> <agent> <unit> <url>` | record the PR for your unit; the lead's watch tracks its CI |
| `post <board> <agent> <TAG> "<message>" [--re ID]` | NOTE, ASK, ANSWER, BLOCKED or DONE; `@name` addresses |
| `find <board> <agent>` | record a finding from JSON on stdin; prints `F<id>` |
| `amend <board> <agent> <finding>` | update your finding from JSON on stdin |
| `confirm <board> <agent> <finding> "<evidence>"` | confirm another agent's finding |
| `contest <board> <agent> <finding> "<evidence>"` | contest another agent's finding |
| `retract <board> <agent> <finding> "<reason>"` | retract your finding |
| `check <board> lead <finding> "<note>"` | lead: record your own verification of a finding |
| `retire <board> lead <agent> [note]` | lead: mark an agent that handed back as done |
| `preflight <board> "<text>"` | lead: setup notes every join prints |
| `read <board> [--since ID] [--agent A] [--tag T]` | print posts |
| `status <board>` | units, agents, findings, open asks |
| `units <board>` | every unit with owner and verdict |
| `findings <board> [--all] [--json] [--md]` | findings by severity |
| `open-asks <board> [agent]` | ASKs with no ANSWER |
| `sql <board> "<query>"` | read-only SQL |
| `link <board> <branch>...` | record branches this swarm shipped |
| `branches <board>` | list linked branches |
| `watch <board> [--digest SECONDS] [--interval SECONDS]` | the lead's Monitor stream |
| `close <board>` | close the board; the watch exits |
| `archive <board>` | move the .db to `~/.claude/boards/archive/` |

Ids accept `7`, `#7` or `F7`.

A finding is one JSON object on stdin:

```bash
board.sh find checkout-latency db-helper <<'EOF'
{"title": "N+1 query in cart totals", "category": "performance", "severity": "high",
 "status": "VERIFIED", "evidence": "api/cart.py:88; slow log 14:02-14:40",
 "fix": "load line items in one query", "effort": "S", "impact": "high", "unit": 2}
EOF
```

Categories: `security`, `performance`, `correctness`, `design`, `dry`,
`simplification`, `dead-code`, `rule-violation`, `other`. Severity and impact:
`high`, `med`, `low`. Status: `VERIFIED` or `LIKELY`. Effort: `S`, `M`, `L`,
`S-M`, `M-L`.

## Board tables

| Table | Holds |
| --- | --- |
| `meta` | goal, lead name, preflight notes, closed flag, lead's last activity |
| `agents` | name, role (lead, helper, captain), lane, status, joined, last active, last post seen, last nudge |
| `units` | title, scope, parent, owner, state (open, claimed, done, dropped), verdict, brief file, dependency, PR URL, CI state |
| `posts` | author, tag (NOTE, ASK, ANSWER, BLOCKED, DONE, plus automatic UNIT, VERDICT, ASSIGN, FOUND, AMEND, CONFIRM, CONTEST, RETRACT, CHECK, PR), body, mentions, reply-to post, related finding |
| `findings` | unit, author, title, category, severity, status (VERIFIED, LIKELY, RETRACTED), evidence, fix, effort, impact, confirmed by, contested by, lead check, retraction reason |
| `branches` | branch names the swarm shipped |

The database runs in WAL mode with a 15-second busy timeout, so many agents can
write at once. Agents never write SQL; every write goes through a subcommand.

## Files

```text
bin/board.py               CLI and schema
bin/board.sh               wrapper that runs board.py
hooks/board-inbox.sh       PostToolUse hook that delivers posts
hooks/hooks.json           hook registration for the plugin install
skills/swarm-board/SKILL.md  the lead's playbook
.claude-plugin/            plugin and marketplace manifests
install.sh                 install into ~/.claude without the plugin system
```

## License

MIT. See [LICENSE](LICENSE).
