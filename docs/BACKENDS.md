# Backends

A **backend** fills a seat: the subject (the thing under test) or the sim (the simulated person). Every
seat can use any backend. Nothing outside a backend's own file assumes one vendor.

## Writing a seat

In `case.toml`, a seat is a short string, `backend[:model]`:

| String | Means |
|---|---|
| `mock` | scripted replies, for testing the tool. Costs nothing. Runs are marked "plumbing only". |
| `claude:sonnet` | the `claude` command line program, with that model (the model is optional) |
| `http:MODEL@BASE_URL` | any OpenAI-compatible endpoint, such as `http:llama3@http://localhost:11434/v1` |
| `openai:MODEL` | `https://api.openai.com/v1`, key `OPENAI_API_KEY` |
| `openrouter:MODEL` | `https://openrouter.ai/api/v1`, key `OPENROUTER_API_KEY`, 18 requests a minute |
| `deepseek[:MODEL]` | `https://api.deepseek.com`, key `DEEPSEEK_API_KEY`, model `deepseek-chat` by default |

Or as a table, when a seat needs more settings:

```toml
[sims]
seat = { backend = "http", model = "my-model", base_url = "http://127.0.0.1:8080/v1",
         key_env = "MY_KEY", price_in_per_mtok = 0.30, price_out_per_mtok = 1.20,
         timeout = 120, rpm = 30 }
```

| Setting | Meaning |
|---|---|
| `backend`, `model`, `base_url` | as above; a preset name such as `deepseek` fills in the rest |
| `key_env` | the name of the key (default `OPENAI_API_KEY`) |
| `price_in_per_mtok`, `price_out_per_mtok` | dollars per million tokens. With both, cost is worked out from the tokens the server reports. Without them, cost is recorded as unknown (never zero) |
| `timeout` | seconds to wait for one call |
| `rpm` | at most this many requests a minute to this endpoint |
| `max_tokens`, `temperature` | sent to the server if given |
| `setting_sources`, `max_budget_usd`, `tools`, `permission_mode` | claude only; see below |

## Keys

A key is read from the environment first, then from `~/.config/rehearse/keys`, which holds `NAME=value`
lines (`#` starts a comment). Make that file private: `chmod 600 ~/.config/rehearse/keys`. If other
users can read it, rehearse refuses to use it and says so. A key is never printed and never saved: any
key value that was read is removed from everything written to a run folder. A server on `localhost`,
`127.0.0.1` or `::1` is sent no key unless the seat names one with `key_env`, and the keys file is not
read for it. A key is never sent over plain `http` to another host, and redirects are refused, so a key
cannot be carried to a different address.

## What each backend can do

A backend says what it supports, and the adapters check it before a run starts:

| | chat | agent | resume | fork |
|---|---|---|---|---|
| `mock` | yes | yes | yes | yes |
| `claude` | yes | yes | yes | yes |
| `http` | yes | no | no | no |

- A **chat** seat takes a system prompt and the whole conversation each turn and returns the reply.
  The sim is always a chat seat. A chat subject (`adapter = "chat"`) is one too.
- An **agent** seat works in a folder and keeps a session, so it remembers its own earlier turns and
  tool results. An agent subject (`adapter = "agent"`) needs `agent` and `resume`; the error says
  "backend X cannot run an agent subject" if a backend lacks them. `fork` (continue from a saved point
  without changing it) is used by focused continuations, a later step.
- There is no fallback that replays a transcript into a new agent session. A backend without `resume`
  cannot be an agent subject; it can still be a chat subject.

Every call's raw output (the program's stdout and stderr, or the HTTP body) is saved before it is read,
so a reply that cannot be parsed still leaves its raw text in the run's `raw/` folder.

## The claude backend

The program is `claude`, or whatever `REHEARSE_CLAUDE_BIN` names.

**Chat seats** (the sim, or a chat subject) run as:

```
claude -p --output-format json [--model M] --system-prompt-file FILE --tools ""
       --setting-sources "" --strict-mcp-config --no-session-persistence
```

with the conversation on standard input, in a fresh empty folder. The system prompt is written to a
private file (mode 600, in its own private folder that is not the seat's working folder) and removed
after the call, so it never appears on the command line, where `ps` would show it. Each flag keeps
something out:
`--tools ""` gives no tools, `--setting-sources ""` loads no settings, memory or instruction files,
`--strict-mcp-config` loads no extra servers, `--no-session-persistence` saves no session. If
`ANTHROPIC_API_KEY` is set, `--bare` is added, which also drops the account details a subscription login
puts into every call. Without `--bare`, a subscription login may still put the account's email address and
the machine's environment into the model's context; rehearse scrubs them from what it saves, but the model
sees them.

**Agent seats** run in the work folder as:

```
claude -p --output-format stream-json --verbose [--model M] --setting-sources project
       --strict-mcp-config --tools Read,Write,Edit --permission-mode dontAsk
       [--resume ID [--fork-session]] --allowedTools T1 T2 ...
```

with the message on standard input. The first turn has no `--resume`; later turns resume the same
session. The streamed output gives the reply, every tool call (name and main argument, with the work
folder shown as `./`), and the cost.

- **Tools go on the command line.** A folder that has not been trusted interactively may ignore its own
  settings file, so a tool list in `.claude/settings.json` cannot be relied on. The case's
  `allowed_tools` are always passed as `--allowedTools`.
- **`--allowedTools` only pre-approves; it does not restrict.** An agent allowed `Write(note.md)` once wrote
  its file with `Bash` instead. So the session also passes `--tools` with the distinct tool names from
  `allowed_tools` (`Read,Write,Edit`), so no other tool exists, and `--permission-mode dontAsk`, which
  denies anything not pre-approved. Change them with `tools = [...]` and `permission_mode = "..."` in
  `[subject]` (or in the seat table).
- **Cost is cumulative.** Verified against the real program: `total_cost_usd` is a running total for the
  session (turn two reports turn one plus turn two). Each turn's cost is the growth since the last total
  (`cost_basis: "delta"` in the raw record). If a total ever goes down, that turn's cost is recorded as
  unknown (`"unknown: total decreased"`), not guessed. The first turn after a fork is marked
  `"fork-unverified"`: it continues from the parent's last total, which has not been checked against the
  real program yet.
- **Failure.** A non-zero exit with a usable result is used, and the exit code is recorded. A non-zero
  exit with no result is an error that includes the end of the error output. Quota wording ("usage limit",
  "quota", "rate limit") is looked for only in the error output and in the text of an error result, never
  in the text of a reply that succeeded; a match is a quota error, which stops a whole suite.
- **Timeouts.** The program runs in its own process group. On a timeout or Ctrl-C the whole group is
  killed, so no orphan keeps running.
- Settings (in the seat table): `setting_sources` (default `project`), `timeout` (default 900 seconds),
  `max_budget_usd`. A `--max-usd` on the command line is passed on as `--max-budget-usd`.

## The agent's folder: not a sandbox

For an agent subject, each run copies the case's scenario folder (without the files listed in
`[scenario] exclude`) into a new temporary folder outside the case, lays the variant's folder over it,
and removes it afterwards (`--keep-work` keeps it). The copy is hashed before and after, and the run
folder gets `artifacts/diff.json`, the added and changed files, and a patch.

Exclude patterns work like `.gitignore`: a pattern with no `/` matches that name at any depth
(`*.tmp`, `secret.md`); a pattern with a `/` in it is matched from the scenario folder (`docs/*.md`);
`dir/` and `dir/**` match everything under `dir`. A pattern that matches nothing is reported (printed,
and listed in `inputs/scenario.json`) because it is usually a typo.

Symbolic links are never copied or followed: links in the scenario are skipped, listed under
`skipped_symlinks`, and warned about; a link the agent makes is recorded in `diff.json` as
`{"path": ..., "symlink_to": ...}` and its target is never read. The run refuses to start if the system's
temporary folder is inside the case or scenario folder.

Before the first turn, rehearse refuses to run if the copy holds a file identical to any sim's persona
file or knowledge page, or to the sim frame, or with the same name. Leaving a file out is isolation;
telling the agent not to open it is not.

Files the agent adds or changes are copied byte for byte to `artifacts/files/`. If a text file holds an
email address, the home folder path or a key, the scrubbed text is saved instead and `diff.json` marks
it `"scrubbed": true`. A binary file holding the home folder path or a key is not saved
(`"withheld": "contains private data"`). A file whose permissions changed is listed under `changed` with
a `"mode"`. `diff.patch` covers added and changed text files.

This is **not a sandbox**. The agent's tools run as you. If the case allows `Bash`, or `Read` without a
path, the agent can read files outside its work folder, including your home folder. Allow only the tools
the case needs, and narrow them (`Write(review.md)`).

## Cost and budgets

`rehearse suite CASE --dry-run` lists the runs and estimates their cost from earlier runs of the same
case: the mean known cost per run for each seat, times the runs planned, and how many past runs it is
based on. With none, it says so; a seat with no known cost shows as "unknown", and the total is then
"at least" the known part. Any command that uses a
seat other than `mock` shows the plan and asks "Start N runs? [y/N]". `--yes` skips the question; with no
terminal and no `--yes` the command refuses.

`--max-usd X` (a number above 0) stops starting new runs once the known cost reaches X, and passes the money left to claude
as a per-call limit. Runs that did not start are listed as "not run: budget reached". A seat that does
not report its cost cannot be counted, and rehearse warns when it sees one. At the end of a live command
it prints what each seat cost.
