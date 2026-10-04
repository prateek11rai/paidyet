# DevRelay: installed once per machine, active only in this repo

PaidYet was built in Claude Code with [DevRelay](https://devrelay.com), MLH's gateway to DEV and MLH. DevRelay's default installer does four things:
- it registers its MCP server in every AI tool on the machine;
- it copies its skills into the global skill folders;
- it adds a daily background job that syncs those skills again;
- it adds itself to your shell profiles.

Several of those skills tell the agent to act without being asked. They offer to upload the session transcript to DEV, draft posts, and look up DEV articles. That's useful in a hackathon repo but unwanted everywhere else, such as in work projects. So this repo splits DevRelay in two:

| Piece | Where it lives | Reach |
|---|---|---|
| The binary | `~/.devrelay/bin/` | the machine |
| The login (MyMLH, with DEV linked) | `~/.devrelay/credentials` | the machine |
| The MCP server registration | Claude Code's **local** scope (`~/.claude.json`, under this repo's path) | Claude Code sessions started in this repo |
| The skills | `.claude/skills/devrelay-*/` (gitignored) | this repo |
| The agent rules | `.claude/devrelay-rules.md` (gitignored; `CLAUDE.md` points to it) | this repo |
| The background updater | not installed | none |

The login does nothing on its own. It's used only when a session in an opted-in repo starts the MCP server.

## 1. Once per machine: install and log in

Download the installer and read it before you run it, instead of piping it into `sh`:

```sh
curl -fsSL https://devrelay.com/install.sh -o /tmp/devrelay-install.sh
less /tmp/devrelay-install.sh
sh /tmp/devrelay-install.sh --hosts=none --no-skills --no-auto-update --no-path-update
```

| Flag | What it skips |
|---|---|
| `--hosts=none` | Registering the MCP server in every AI tool it finds (Claude Code, Codex, Cursor and others) |
| `--no-skills` | Copying the skills into `~/.claude/skills`, `~/.agents/skills` and `~/.gemini/config/skills` |
| `--no-auto-update` | The daily background job (`com.devrelay.updater` on macOS) that updates DevRelay and syncs its skills to those global folders |
| `--no-path-update` | Adding `devrelay` to your shell profiles. Call it by its full path instead |

Add `--no-telemetry` to opt out of usage telemetry.

Then log in. The login opens your browser for MyMLH, and your DEV account comes through MLH:

```sh
~/.devrelay/bin/devrelay login
~/.devrelay/bin/devrelay status
```

Check that nothing global was left behind. Each of these should print nothing:

```sh
ls ~/Library/LaunchAgents | grep -i devrelay
ls ~/.claude/skills ~/.agents/skills | grep -i devrelay
```

`claude mcp list`, run outside any opted-in repo, shouldn't show `devrelay-gateway`.

## 2. Once per repo: wire it in

From the repo root:

```sh
uv run poe devrelay-setup
```

The task (see `pyproject.toml`) does three things. In a repo without the task, do them by hand:

1. Register the MCP server at local scope, so only sessions started in this directory see it. `DEVRELAY_SKILLS_DIR` makes the server's own skill sync write here too:
   ```sh
   claude mcp add devrelay-gateway --scope local \
     --env DEVRELAY_SKILLS_DIR="$PWD/.claude/skills" \
     -- "$HOME/.devrelay/bin/dev_mlh_mcp_server" --stdio
   ```
2. Install the skills into this repo only. The sync runs from an empty folder because inside a git repo it also writes `.agents/AGENTS.md`:
   ```sh
   skills="$PWD/.claude/skills"
   (cd "$(mktemp -d)" && DEVRELAY_SKILLS_DIR="$skills" ~/.devrelay/bin/devrelay --sync-skills)
   ```
3. Save the agent rules as `.claude/devrelay-rules.md`. They're the `agents_rules` entry of <https://devrelay.com/v1/skills.json>, minus the last three bullets (stdout, docs, version tags), which are meant for DevRelay's own repository.

A new repo also needs:
- a line in `CLAUDE.md` that points at `.claude/devrelay-rules.md`;
- these lines in `.gitignore`:

```gitignore
.claude/settings.local.json
.claude/skills/devrelay-*/
.claude/devrelay-rules.md
.agents/
```

Restart Claude Code in the repo and run `/mcp`: `devrelay-gateway` should be connected. Start Claude Code anywhere else and it isn't there.

Each Hacktoberfest challenge needs a new repo, so repeat this step in each one.

## Keeping it up to date

- **The binary:** `~/.devrelay/bin/devrelay --self-update`. The MCP server also checks for updates when it starts, at most once a day. To turn that off, add `--env DEVRELAY_DISABLE_AUTO_UPDATE=1` to the `claude mcp add` line.
- **The skills:** run `uv run poe devrelay-setup` again. **Never run `--sync-skills` without `DEVRELAY_SKILLS_DIR`**, because it then writes to the global skill folders.
- **Avoid `devrelay --doctor`.** In 0.1.17 it doesn't only diagnose. It rewrote an existing updater entry so that it runs at login, and that updater immediately synced the skills into the global folders. If you do run it, repeat the checks from step 1 afterwards.

## Removing it from a repo

```sh
claude mcp remove devrelay-gateway --scope local
rm -r .claude/skills/devrelay-*/ .claude/devrelay-rules.md
```

## Why the skills aren't committed

The skills are MLH's text and carry no license, and DevRelay rewrites them whenever it syncs. Each clone fetches its own copy with `uv run poe devrelay-setup`, and `.gitignore` keeps them out of commits.
