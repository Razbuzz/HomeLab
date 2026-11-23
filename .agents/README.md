# HomeLab `.agents/` Directory

This directory holds vendor-neutral, reusable agent role definitions for **Razbuzz/HomeLab**. Any AI coding assistant, CLI agent, or multi-agent orchestrator can load [`../AGENTS.md`](../AGENTS.md) as the shared repository baseline and pair it with one of the role playbooks below.

## Directory Layout

```text
.agents/
├── README.md                  # Organization, invocation guide, and model-tier guidance
└── agents/
    ├── developer.md           # Feature and bugfix implementation
    ├── maintainer.md          # Triage, merging, releases, and dependency hygiene
    ├── researcher.md          # Self-hosted service evaluation and architecture specs
    └── reviewer.md            # Pull request code, infra, and security review
```

Each file in `agents/` uses YAML frontmatter (`name`, `description`, `recommended_tier`, `access`, `scope`) followed by an actionable system prompt and checklist tailored to HomeLab.

## How to Invoke an Agent

### For Human Operators
Point your coding assistant to [`AGENTS.md`](../AGENTS.md) and the desired role file, then state the task:
- **Implementing a change:** "Read `AGENTS.md` and `.agents/agents/developer.md`, then implement issue #..."
- **Reviewing a PR:** "Read `AGENTS.md` and `.agents/agents/reviewer.md`, then review PR #..."
- **Evaluating a new service:** "Read `AGENTS.md` and `.agents/agents/researcher.md`, then evaluate `<service>` for HomeLab."
- **Managing merges or releases:** "Read `AGENTS.md` and `.agents/agents/maintainer.md`, then prepare release `vX.Y.Z`."

### For Orchestrators & Subagent Workflows
1. Always supply [`AGENTS.md`](../AGENTS.md) as repository context.
2. Load the matching `.agents/agents/<role>.md` file as the role system prompt.
3. Enforce the `access` boundary declared in frontmatter (`read-only`, `read-only-web`, `read-write`, `repo-maintainer`) and isolate write work in dedicated git worktrees.

## Model-Tier Guidance

| Recommended Tier | When to Use | Applicable Roles & Tasks |
| :--- | :--- | :--- |
| **`high`** (frontier reasoning) | Architecture decisions, security-sensitive shell/compose changes, PR review, release orchestration, and deep service research. | `developer`, `reviewer`, `maintainer`, `researcher` |
| **`standard` / `fast`** (lighter models) | Narrow, well-specified mechanical edits where architecture is already settled (e.g., typo fixes, link checks, formatting updates, or summarizing CI logs). | Scoped documentation updates or mechanical chores following an approved spec |

Do not downgrade `reviewer`, `maintainer`, or security-sensitive `developer` tasks to a lower tier—subtle regressions in volume permissions, port collisions, or `.env` parity are easily missed without thorough reasoning.
