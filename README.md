# Sherpa

Sherpa is a dispatcher for a personal toolbox. Every tool in `tools/` is a standalone
[PEP 723](https://peps.python.org/pep-0723/) script — its dependencies are declared in its own
header and installed on demand by `uv run --script`. The `sherpa` CLI finds the tool, runs it in
that isolated environment, and gets out of the way. Adding a tool that needs `playwright` cannot
break a tool that needs `boto3`, because they never share an environment.

Tools are built for agents to call, not for humans to admire: structured output, strict flag
parsing, machine-readable errors. That contract is the [AXI standard](https://axi.md/), spelled
out for this repo in [`docs/SHERPA_STANDARDS.md`](docs/SHERPA_STANDARDS.md).

## Install

Prerequisites:

- [`uv`](https://docs.astral.sh/uv/) on your `PATH`
- Python 3.11 or newer
- `~/.local/bin` on your `PATH` (where `uv tool install` puts the `sherpa` binary)

```bash
git clone <this-repo> ~/src/sherpa
uv tool install ~/src/sherpa/cli
sherpa set-home ~/src/sherpa
```

**Install `cli/`, not the repo root.** The repo root is a separate project with its own
`pyproject.toml`; installing it gives you no `sherpa` command and is the single most common way
to get stuck here.

`sherpa set-home` writes the clone path to `~/.config/sherpa/config.toml`. The `SHERPA_HOME`
environment variable overrides that file when set, which is how you point a shell at a second
checkout without disturbing the default.

Verify:

```bash
sherpa where          # prints the resolved home
sherpa list           # every tool with its one-line description
sherpa search slack   # search by name, description, category, usage
sherpa help notify    # usage block plus the tool's own --help
```

Then run anything: `sherpa <tool> [args...]`.

## Trimming what shows up

A toolbox this size is a lot to hand a newcomer at once, so tools can be withheld from
discovery:

```bash
sherpa sherpa_admin hide youtube unsplash_search   # drop them from list/search/tool_search
sherpa sherpa_admin show youtube                   # put one back
sherpa sherpa_admin status                         # what's active, what's hidden, where config lives
sherpa list --all                                  # everything, hidden ones marked
```

Hidden is not disabled: `sherpa <tool>`, `sherpa help <tool>` and `tool_run` still work on a
hidden tool. This is progressive disclosure, not access control. The list lives under `hidden`
in `~/.config/sherpa/config.toml`, and `sherpa list` always reports how many it withheld.

## Secrets

Tools declare the vault keys they need in their docstring header:

```yaml
secrets: [SLACK_USER_TOKEN]
```

When a key is missing, the tool prints `MISSING_SECRET: <KEY>` on stderr and exits `2`. The
recovery is always the same three steps:

```bash
sherpa vault_manager set SLACK_USER_TOKEN xoxp-...
```

then retry the original command. The vault lives at `~/.sherpa/vault.json` — local to your
machine, never committed.

## Using Sherpa from an agent

`.mcp.json` defines an MCP server exposing two tools:

- `tool_search(query)` — find tools by keyword
- `tool_run(tool_name, args)` — run one

Its command is `uv run --directory . python -m sherpa.server`. The `.` is relative to this repo,
so **when you wire the server into another project, replace `.` with the absolute path to your
Sherpa clone** — otherwise the server starts in the wrong directory and finds no tools.

### For an agent that authorizes before it runs

`tool_run` takes a tool name and an argument string, which is the right shape for
an assistant a person is watching and the wrong one for an agent deciding on its
own whether it may act — a single entry point cannot be read-only for
`jira_issues get` and destructive for `jira_issues transition`.

Tools can therefore declare their subcommands: a tier (`read` / `write` /
`dangerous`) and a command-line template per subcommand, under `operations` in
the docstring. See [`docs/SHERPA_STANDARDS.md`](docs/SHERPA_STANDARDS.md).

```bash
sherpa manifest    # JSON: every tool that declares operations, plus the names of those that do not
```

Declaring changes nothing about how a tool runs — `sherpa <tool>` and `tool_run`
are unaffected. It only makes the tool visible to a caller that has to know, in
advance, what a subcommand costs. `writ` reads this to register each declared
tool as a separate capability with its own risk tier and approval rules.

`CLAUDE.md` at the repo root is the agent-facing companion to this file: discovery, running
tools, and the missing-secret recovery loop. If your agent has no MCP support, paste this into
its instructions and let it use the shell directly:

```markdown
## Sherpa tools

`sherpa` is a CLI dispatcher for a set of prebuilt tools (Jira, Slack, Sentry, notes, web, ...).

- `sherpa search <keyword>` — check for an existing tool before building anything
- `sherpa list` — everything available
- `sherpa help <tool>` — usage for one tool
- `sherpa <tool> [args...]` — run it

If a tool prints `MISSING_SECRET: KEY` on stderr, ask me for the value, run
`sherpa vault_manager set KEY VALUE`, then retry.
```

## Retired tools

Some tools in `tools/` are tombstones: they print migration guidance and exit non-zero, on
purpose. `sherpa fleet`, for example, tells you its orchestration moved to `lb`. A non-zero exit
from one of these is the tool working correctly, not a broken install.

## Contributing

Read [`docs/SHERPA_STANDARDS.md`](docs/SHERPA_STANDARDS.md) first — it is the contract, including
the docstring schema, the shared `sherpa/render.py` output boundary, exit codes, and a new-tool
checklist. The [`axi` skill](.agents/skills/axi/SKILL.md) covers the design principles behind it.

To add a tool, drop a `.py` file in `tools/`. It is picked up on the next `tool_search`. To force
an index rebuild:

```bash
sherpa reindex
```
