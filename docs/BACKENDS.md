# Backends

A **backend** fills a seat: the subject (the thing under test) or the sim (the simulated person). Every
seat can use any backend. Nothing outside a backend's own file assumes one vendor.

## Writing a seat

In `case.toml`, a seat is a short string, `backend[:model]`:

| String | Means |
|---|---|
| `mock` | scripted replies, for testing the tool. Costs nothing. Runs are marked "plumbing only". |
| `claude:sonnet` | the `claude` command line program, with that model (the model is optional) |
| `opencode:PROVIDER/MODEL` | the `opencode` command line program (agent seats only), such as `opencode:deepseek/deepseek-chat` |
| `codex[:MODEL]` | the `codex` command line program (agent seats; chat seats through an agent wrapper), such as `codex:gpt-5` |
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
| `provider`, `permission` | opencode only; see below |
| `sandbox`, `config` | codex only; see below |

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
| `opencode` | no | yes | yes | yes |
| `codex` | yes (wrapped) | yes | yes | yes |
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

## The opencode backend

The program is `opencode`, or whatever `REHEARSE_OPENCODE_BIN` names. It is an agent backend: it can run
an agent subject but not a chat seat or the sim. Tested against opencode 2.0.x.

**Seat.** `opencode:PROVIDER/MODEL`. For the provider names `deepseek`, `openai` and `openrouter`,
rehearse knows the endpoint and key name, so `opencode:deepseek/deepseek-chat` needs no table. For anything
else, use a table:

```toml
[subject]
seat = { backend = "opencode", model = "mine/m1", price_in_per_mtok = 0.30, price_out_per_mtok = 1.20,
         timeout = 600, provider = { base_url = "http://127.0.0.1:8080/v1", key_env = "MY_KEY",
                                     npm = "@ai-sdk/openai-compatible", models = ["m1", "m2"] } }
```

`provider` takes `base_url`, `key_env`, `npm` (default `@ai-sdk/openai-compatible`) and `models` (the model
id in the seat is always included). A provider that opencode already knows, with no `provider` table here,
gets no provider configuration from rehearse.

**A turn** runs in the work folder as:

```
opencode run --standalone --format json -m PROVIDER/MODEL [-s SESSION_ID [--fork]] -- "message"
```

The message is an argument (after `--`, so it may start with `-`). The first turn has no `-s`; later turns
continue the same session. The output is one JSON event per line. The reply is the text parts after the
last tool call (all text parts, if there are none after it), joined with blank lines. Each tool call is
recorded as its name and main argument (path, command, pattern or name), with the work folder shown as `./`.
Unknown event types are ignored.

- **Isolation.** Each session gets a private, empty `HOME` (removed when the session closes; a fork shares
  its parent's, because the forked session lives there), `OPENCODE_DISABLE_AUTOUPDATE=1`, and no `XDG_*`
  variables. All configuration goes in one variable, `OPENCODE_CONFIG_CONTENT`, so nothing from the
  person's own opencode setup reaches the agent. Project skills (`.claude/skills`, `.agents/skills`,
  `.opencode/skills`) and `AGENTS.md` are still read from the work folder.
- **Keys.** Read the usual way (see Keys) and put only in the program's environment. The configuration
  names the key as `{env:NAME}`, so its value is never in the configuration, the command line or a saved file.
- **Instructions.** opencode obeys `AGENTS.md` but, with a clean `HOME`, not `CLAUDE.md`. If the work folder
  has a `CLAUDE.md` and no `AGENTS.md`, rehearse adds `"instructions": ["CLAUDE.md"]` and records
  `instructions_added` in the turn's extras.
- **Permissions.** The case's `allowed_tools` (written Claude-style) become an opencode `permission` table
  that denies everything else (`"*": "deny"`). A denied tool is not offered to the model at all.

  | allowed tool | opencode permission |
  |---|---|
  | `Read` | `read` |
  | `Glob`, `Grep` | `glob`, `grep` |
  | `Write(X)`, `Edit(X)` | `edit` allowed for `X` only (`edit` covers write, edit and patch) |
  | `Write`, `Edit` | `edit` allowed |
  | `Bash(cmd *)` | `bash` allowed for `cmd *` only (`cmd:*` is read as `cmd *`) |
  | `Skill` | `skill` |
  | `WebFetch`, `WebSearch` | `webfetch`, `websearch` |
  | anything else | no counterpart; stays denied |

  `skill` is always allowed when the work folder has a skills folder. To write the table yourself, put
  `permission = { ... }` in `[subject]` (or the seat table); it replaces the mapping. The permissions used
  are recorded in each turn's extras.
- **Cost.** opencode reports a cost of 0 when it has no prices, so rehearse never records that as known.
  A non-zero reported cost is used. Otherwise, with `price_in_per_mtok` and `price_out_per_mtok` in the seat
  table, cost is worked out from the summed tokens (input plus cache reads and writes, at the input price;
  output plus reasoning, at the output price). With neither, cost is unknown.
- **Fork.** `-s ID --fork` makes a new session id that keeps the old context; the fork tracks the new id and
  the original is unchanged.
- **Failure.** A line that is not a JSON object is an error that keeps the raw output. A non-zero exit with
  events is used and the exit code recorded; with no events it is an error with the end of the error
  output. Quota wording is looked for only in the error output and in `error` events. On a timeout or Ctrl-C
  the program's whole process group is killed.
- `timeout` (default 900 seconds) is the one setting. `max_budget_usd` is not passed on: opencode has no
  such flag, so `--max-usd` cannot stop a turn that is already running.

## The codex backend

The program is `codex`, or whatever `REHEARSE_CODEX_BIN` names. It is an agent backend. It can also fill
a chat seat (a sim or the judge): each call is a fresh `codex exec` thread in an empty, read-only temporary
folder, with the system prompt and the conversation so far on stdin. codex has no plain chat mode, so its own
agent instructions still wrap the prompt; say so when comparing a codex sim or judge with a true chat seat.
Tested against codex-cli 0.163.0 with a ChatGPT login.

**Seat.** `codex` or `codex:MODEL`. Without a model, codex uses its own default. As a table:

```toml
[subject]
seat = { backend = "codex", model = "gpt-5", timeout = 600, sandbox = "workspace-write",
         config = ['model_reasoning_effort="low"'] }
```

`sandbox` overrides the choice described below. `config` is a list of extra `-c key=value` settings passed
to codex. `key_env`, `price_in_per_mtok` and `price_out_per_mtok` are for cost (see Cost).

**A turn** runs in the work folder (the process's folder and `PWD`), with standard input closed, as:

```
codex exec [resume|fork] --json --skip-git-repo-check --ignore-user-config --ignore-rules [-C WORKDIR]
      -c sandbox_mode="workspace-write"
      -c sandbox_workspace_write.exclude_slash_tmp=true
      -c sandbox_workspace_write.exclude_tmpdir_env_var=true [-m MODEL] [THREAD_ID] [--] "message"
```

The first turn has no subcommand and passes `-C WORKDIR`; later turns use `exec resume ... THREAD_ID`,
which refuses `-C`, so resume and fork rely on the process's folder and `PWD`. `exec fork ... THREAD_ID`
starts a new thread that keeps the old context; the fork tracks the new thread id and the original is
unchanged. The message is the last argument, after `--` if it starts with a dash. Standard input is always
closed, because codex otherwise waits for more input. The output is one JSON event per line. The reply is
the last `agent_message` of the turn; earlier ones are kept in the turn's extras as `preamble`.

- **The `/tmp` exclusions are essential.** Work copies live under the system temporary folder. With plain
  `workspace-write`, an agent wrote `../outside.txt` next to its work copy, because that sandbox leaves the
  temporary folders writable. With the two exclusions, writes outside the work folder were refused. They
  are passed on every turn, whatever the sandbox.
- **Isolation.** Each session gets a private `CODEX_HOME` (a temporary folder, mode 700, shared with its
  forks and removed when the last one closes). The person's login file (`$CODEX_HOME/auth.json`, else
  `~/.codex/auth.json`) is copied into it, mode 600, by file copy only: rehearse never reads it, and it and
  the private home are never copied into a run folder. If there is no login file, the run stops with
  "log in with `codex login` first". `--ignore-user-config` and `--ignore-rules` keep the person's own
  codex settings and rules out. A harmless warning about "PATH aliases" under a temporary folder may appear.
- **Tool limits are approximate.** codex has no per-tool or per-path list. The case's `allowed_tools` only
  choose the sandbox: `workspace-write` if any of `Write`, `Edit`, `MultiEdit`, `NotebookEdit` or `Bash` is
  allowed (with or without a path), else `read-only`. `Write(review.md)` therefore does not stop the agent
  writing other files in its work folder, and an agent in either mode can run shell commands. Each run prints
  one warning saying so, and the turn's extras hold `tool_limits` ("approximate: sandbox MODE"). The
  work-folder check and the folder diff still apply and are the real safeguards.
- **Instructions.** codex obeys `AGENTS.md` in the work folder. If the folder has a `CLAUDE.md` and no
  `AGENTS.md`, rehearse adds `-c project_doc_fallback_filenames=["CLAUDE.md"]` and records
  `instructions_added`. Skill folders (`.claude/skills`, `.agents/skills`) are listed in the extras as
  `skills_present`; whether codex loads them is **not verified**.
- **Tool calls.** A command is recorded as `shell` with the command (the `/bin/bash -lc '...'` wrapper
  removed, the work folder shown as `./`). A file change is one call per file: `write` (added), `edit`
  (updated) or `delete`, with the path. Any other item type (such as `web_search`) is recorded under its
  type name with no argument. The work-folder check treats `shell` like `bash` and these like file tools.
- **Model.** The events never name the model. If the seat names one, that is used. Otherwise rehearse looks
  in the private home's `sessions/` folder for the file of the thread and reads the first `"model"` value;
  failing that, the model is unknown.
- **Tokens and cost.** `tokens_in` is codex's `input_tokens` (which already includes cached tokens);
  `tokens_out` is output plus reasoning tokens, summed over the turn. Cost is unknown, never zero. With a
  ChatGPT login the turn's extras say `cost_basis: "subscription (not metered)"`. If the seat names an API
  key with `key_env` (read the usual way, passed to codex as `OPENAI_API_KEY`, and no login file is copied;
  this path is not verified live), cost is worked out from `price_in_per_mtok` and `price_out_per_mtok`.
- **Failure.** A `turn.failed` event, or an `error` event with no completed turn, is an error; quota wording
  makes it a quota error. A non-zero exit with events is used and the exit code recorded; with none it is an
  error with the end of the error output. On a timeout or Ctrl-C the whole process group is killed.
- `timeout` (default 900 seconds). `max_budget_usd` is not passed on: codex has no such flag.

## The agent's folder: not a sandbox

For an agent subject, each run copies the case's scenario folder (without the files listed in
`[scenario] exclude`) into a new temporary folder outside the case, lays the variant's folder over it,
and removes it afterwards (`--keep-work` keeps it). The copy is hashed before and after, and the run
folder gets `artifacts/diff.json`, the added and changed files, and a patch.

Exclude patterns work like `.gitignore`: a pattern with no `/` matches that name at any depth
(`*.tmp`, `secret.md`); a pattern with a `/` in it is matched from the scenario folder (`docs/*.md`);
`dir/` and `dir/**` match everything under `dir`. A pattern that matches nothing is reported (printed,
and listed in `inputs/scenario.json`) because it is usually a typo.

Symbolic links are never followed. A relative link that stays inside the scenario or variant folder is
recreated as the same link (for a skill folder linked under a second name); any other link is skipped,
listed under `skipped_symlinks`, and warned about; a link the agent makes is recorded in `diff.json` as
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
