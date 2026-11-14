---
name: swarm-board
description: "Coordinate a swarm of subagents through a shared SQLite board. Use when fanning out two or more agents on overlapping work, such as research, code review, or production debugging, where one agent's findings help the others."
---

# Swarm board

You are the **lead**. You fan out helpers, and they coordinate through one SQLite
file per swarm at `~/.claude/boards/<task-slug>.db`. Everyone drives it only
through `board.sh` (run `board.sh -h`). If `board.sh` is not on your PATH, call
it as `~/.claude/bin/board.sh`, and write it that way in every helper prompt.

The board holds the task in progress. It lives until the work is done and every
PR the swarm opened is merged or closed.

**Tables.** `agents` (name, role, status), `units` (claimable work, with
`parent_id` so a unit can split into a tree), `findings` (the deliverable:
category, severity, VERIFIED/LIKELY, evidence, fix, confirmations), `posts`
(chat: NOTE, ASK, ANSWER, BLOCKED, DONE, plus automatic posts for findings,
claims and assignments), `branches`.

**Roles.** You are the `lead`: you allocate, unblock, verify and report. You do
not take units yourself. Everyone you spawn is a `helper`. A helper becomes a
`captain` (`join --role captain`) when its area is too big for one agent: it
splits its unit into child units for others to claim and may `assign` them.
Subagents cannot spawn subagents, so captains delegate through units, never by
spawning.

## Setup, before spawning

1. `board.sh init <board> "<goal>"`. The caller becomes `lead`.
2. Seed units small enough for one agent to finish:
   `board.sh unit <board> lead "<title>" --scope <paths> --brief <spec file>`.
   Seed more units than agents, so fast agents keep pulling work. Do not
   pre-assign fixed lanes. Use `--after <id>` when a unit must wait for another
   (merge order, shared files); `claim` skips it until that unit is done. Each
   brief says what is in scope and what would make the unit unsafe.
3. `board.sh preflight <board> "<setup notes>"`: tool versions and environment
   every agent needs, for example `use python 3.12 via uv; run tests with make
   test; never touch the production config`. Every `join` prints it, so agents
   stop rediscovering the same setup failures.
4. Give each agent a short name and its own scratch subfolder.
5. Paste the agent protocol below into every helper prompt, with the board name
   and the agent's name filled in, plus the shared vocabulary (finding
   categories, what counts as evidence). State the unit cap as a number ("at
   most 2 units"), never as a condition.
6. When the task opens PRs, `board.sh link <board> <branch>...`.

## Agent protocol (paste into each helper prompt)

Replace `<board>` and `<name>` before pasting.

```text
You are helper <name> on swarm board <board>. Coordinate only through board.sh.
- First command: `board.sh join <board> <name>`. It prints the goal and current
  state. From then on a hook appends new board posts to your context after every
  tool call and flags the ones addressed to you. Never edit the .db file or
  write SQL.
- Scout first, before writing any code: `board.sh claim <board> <name>` takes the
  next open unit atomically (or `claim <board> <name> <id>`) and prints its brief
  path; read the brief. Check open PRs touching the same files
  (`gh pr list --search <path>`). Record
  `board.sh verdict <board> <name> <id> "<payoff, size, risks>"`, and post your
  top 2-3 hypotheses as a NOTE so others can contradict them early. If the unit
  is not worth it, `drop` it with the reason (do not `release` it; released
  units come back as claimable). Judge hard areas by evidence, not by size.
- Code-change units: right after opening the PR, run
  `board.sh pr <board> <name> <unit> <url>` so the lead's watch tracks its CI,
  and stay until CI is terminal. Fix red checks yourself; `finish` the unit only
  when it is green.
- Work loop: dig into the unit, record each finding with
  `board.sh find <board> <name>` and a JSON object on stdin (title, category,
  severity high|med|low, status VERIFIED|LIKELY, evidence "path:line; ...", fix,
  effort S|M|L, impact, unit), then `finish` the unit and claim the next. Too
  big? Add child units with `board.sh unit ... --parent <id>`, keep one, and let
  others claim the rest. Not worth it? `drop <id> "<reason>"`.
- Before recording a finding, re-trace it and try to refute it. Wrong later?
  `amend` or `retract` your own; for someone else's, `confirm` or `contest` with
  evidence.
- Questions: `board.sh post <board> <name> ASK "@who ..."` (or `@all`); answer
  with `board.sh post <board> <name> ANSWER --re <ask id> "..."`. Answer ASKs
  aimed at you before continuing. Waiting on an answer? Post BLOCKED and claim
  other work meanwhile.
- No units left and none to split? Post DONE with a one-line summary. DONE is
  refused while you hold a claimed unit or owe an ANSWER. Before DONE, confirm or
  contest at least one other agent's finding that overlaps your area.
- You may take at most <N> units.
- Your final reply restates your finding ids and anything the board could not
  hold.
```

Finding categories the CLI accepts: `security`, `performance`, `correctness`,
`design`, `dry`, `simplification`, `dead-code`, `rule-violation`, `other`.

## Lead duties: oversee continuously

- Right after spawning, start the `Monitor` tool on `board.sh watch <board>` with
  the maximum timeout, and re-arm it every time it expires until the board is
  closed. Never leave a live swarm unwatched.
- The watch streams others' FOUND, ASK, ANSWER, BLOCKED, DONE, RETRACT, CONFIRM,
  CONTEST and PR posts, any post that mentions `@lead` (as FOR LEAD), and
  `CI GREEN` / `CI RED` for every unit with a recorded PR (polled each minute
  through `gh`). It raises alerts (an unanswered ASK to you, a BLOCKED agent, an
  IDLE agent with no work left, a STALE claim from a silent agent, a STRANDED ASK
  to a finished agent), prints a DIGEST every 5 minutes, and escalates any alert
  you have not acted on within 5 minutes as `LEAD OVERDUE`. Any board write by
  you counts as acting.
- On every DIGEST: run `board.sh status <board>`, then rebalance. Split hot
  units, drop dead ones using the scout verdicts, `assign` work to idle agents,
  and override verdicts that give up on winnable work.
- On every alert: act within the 5 minutes. Posts addressed to an agent reach it
  through the hook, so do not relay them with SendMessage. Use SendMessage only
  for an agent that has gone quiet or finished.
- When one agent's question reveals a rule every agent needs, post the rule to
  `@all` instead of only answering the one ASK. Word rulings precisely (exactly
  what is disallowed and why); agents treat a lead answer as precedent and
  over-generalize loose wording.
- An agent that hands back without posting DONE (often while its CI is still
  running) keeps raising IDLE/STALE alerts: finish its unit on `CI GREEN`, then
  `board.sh retire <board> lead <name>`.
- Verify every high or med finding yourself before it reaches the user, and
  record it with `board.sh check <board> lead <id> "<what you read>"`. The
  watch's ALL AGENTS DONE line counts what is still unchecked.
- Talk to the user in batches: one update when the swarm finishes (or when
  something urgent comes up), never one message per post.

## Closing

- When the agents finish, `board.sh findings <board> --md` feeds your report,
  and `board.sh close <board>` ends the watch.
- When the task is finished (every linked PR merged or closed),
  `board.sh archive <board>` moves the .db to `~/.claude/boards/archive/`.
