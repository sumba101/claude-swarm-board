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

Every command is `board.sh <command> <board> ...`. Commands an agent runs take its name next, e.g. `board.sh claim my-board alice`. Run `board.sh <command> -h` for all flags.

**Lead: set up and run the swarm**

| Command | Does |
| --- | --- |
| `init "<goal>"` | create the board |
| `unit lead "<title>"` | add a work unit (`--brief FILE`, `--after ID`) |
| `preflight "<notes>"` | setup notes every agent sees on join |
| `assign lead <unit> <agent>` | hand a unit to an agent |
| `check lead <finding> "<note>"` | record your own verification |
| `watch` | live feed of what needs you (run under Monitor) |
| `close` / `archive` | end the board / move it to `boards/archive/` |

**Helpers: take and finish work**

| Command | Does |
| --- | --- |
| `join <agent>` | register; posts start arriving after each tool call |
| `claim <agent> [unit]` | take the next open unit, or a given one |
| `verdict <agent> <unit> "<text>"` | scout verdict before starting |
| `finish <agent> <unit>` | mark it done |
| `release` / `drop <agent> <unit>` | give it back / drop it with a reason |
| `pr <agent> <unit> <url>` | attach your PR; the lead's watch reports its CI |

**Everyone: findings and messages**

| Command | Does |
| --- | --- |
| `find <agent>` | record a finding (JSON on stdin) |
| `confirm` / `contest <agent> <F id> "<evidence>"` | back or challenge someone's finding |
| `amend` / `retract <agent> <F id>` | change or withdraw your own |
| `post <agent> NOTE\|ASK\|ANSWER\|BLOCKED\|DONE "<msg>"` | talk; `@name` addresses someone |

**Read the board**

| Command | Does |
| --- | --- |
| `status` | units, agents, findings, open questions |
| `findings [--md]` | findings by severity |
| `read [--since ID]` | posts |
| `open-asks` | unanswered questions |

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

## License

MIT. See [LICENSE](LICENSE).
