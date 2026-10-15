# Pitcrew

**A pit crew for Claude Code: it picks, checks and sets up the right tools for your project before you start.**

There are hundreds of skills, MCP servers and configs for Claude Code. Which ones fit your project, which ones are maintained and which ones are safe changes every week. Pitcrew interviews you about your project, researches what exists today, verifies every tool and builds the full setup: `CLAUDE.md`, skills, MCP servers and manual steps. You approve it, and it installs. Claude drives; Pitcrew gets everything around it ready.

> The tool currently speaks Spanish: the interview, the plan and the approval prompts are in Spanish. Full documentation in Spanish: [`README.es.md`](README.es.md).

## What you get

- **A setup built for your project.** Not a generic pack of 300 skills: only what solves something you told it, each piece with a reason to be there.
- **Live research.** It looks for what exists today, not what was trending months ago.
- **Trust that is computed, not guessed.** A verifier with no AI checks every tool on GitHub and assigns it a level. No text on the internet can talk it into a higher one.
- **You have the final say.** Nothing installs without your approval in your own terminal, and every run can be undone with one command.

## How it works

```mermaid
flowchart LR
    A[Describe your project] --> B[Interview]
    B --> C[Live research]
    C --> D[Verification, no AI]
    D --> E[Visual plan]
    E --> F[You approve and install]
```

## Install

You need Python 3.12+, [pipx](https://pipx.pypa.io/), Claude Code and, for MCP servers launched with `npx`, Node and npm.

```bash
git clone https://github.com/gabrielCastorena/pitcrew.git ~/pitcrew
pipx install --editable ~/pitcrew
claude plugin marketplace add gabrielCastorena/pitcrew
claude plugin install pitcrew@pitcrew
```

Recommended: export a read-only `GITHUB_TOKEN` so the verifier does not hit GitHub's rate limit.

```bash
export GITHUB_TOKEN=<your token>
```

Keep the clone where it is: the install is editable because the verifier's config lives there.

## Usage

**1.** Inside your project, in Claude Code:

```
/pitcrew:pitcrew
```

Answer the interview and review the plan as text or as a visual page.

**2.** Apply it yourself, in your terminal:

```bash
pitcrew apply plan.json --project .
```

Changed your mind? `pitcrew revert <run_id> --project .` undoes it.

## Trust levels

| Level | Meaning | On install |
| --- | --- | --- |
| Verificado (verified) | Official publisher, recently active | In one batch, with a single approval |
| Confiable (trusted) | Third party, healthy and low risk | In one batch, with a single approval |
| Revisar (review) | Something doesn't add up, or couldn't be checked | Asks you about each one |
| Bloqueado (blocked) | Has a red flag | Never installs |

Popularity never decides a level. Every MCP server and everything the plan writes, including the `CLAUDE.md`, is confirmed on its own.

## Secure by design

- What is read from the internet never runs commands: the researchers can only search and read.
- The verifier uses no AI model: its verdict comes from hard data on GitHub, so a prompt injection cannot raise a level.
- A plan can only hold five typed operations. There is no way to sneak in a shell command.
- Secrets are asked for in your terminal and never pass through the conversation with Claude.
- Every step is recorded in a manifest you can audit or revert.

## Known limitations

This is an early version. The main ones:

- The `CLAUDE.md` is written from the whole plan, so it can mention a piece you later rejected. The installer warns you and names it.
- Manual steps are only printed, not installed, but the manifest records them as done.
- The recommendation evals have not been run yet.
- Only GitHub repos can be verified.

The full list, and the design and threat model, are in [`README.es.md`](README.es.md) and [`docs/DESIGN.md`](docs/DESIGN.md) (Spanish).

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/python -m pytest -q -W error
```

No test touches the network or your `~/.claude`. To try plugin changes without installing: `claude --plugin-dir ~/pitcrew/plugin`.

## Security and license

Found a vulnerability? Report it privately, as described in [`SECURITY.md`](SECURITY.md). MIT licensed.
