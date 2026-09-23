# ISSUES

Open GitHub issues labelled `ready-for-agent` are provided at start of context as JSON. Parse them to understand the open issues. Each issue's `comments` contain notes left by previous iterations — read them.

The `ready-for-agent` label is the gate. Only issues carrying it are yours to work on. Never work on an unlabelled issue. Never add `ready-for-agent` yourself: `ralph/promote.sh` promotes `ralph:queued` issues automatically once their blockers are done.

You've also been passed a file containing the last few commits. Review these to understand what work has been done.

If all `ready-for-agent` tasks are complete, output <promise>NO MORE TASKS</promise>.

# TASK SELECTION

Pick the next task. Prioritize tasks in this order:

1. Critical bugfixes
2. Development infrastructure

Getting development infrastructure like tests and types and dev scripts ready is an important precursor to building features.

3. Tracer bullets for new features

Tracer bullets are small slices of functionality that go through all layers of the system, allowing you to test and validate your approach early. This helps in identifying potential issues and ensures that the overall architecture is sound before investing significant time in development.

TL;DR - build a tiny, end-to-end slice of the feature first, then expand it out.

4. Polish and quick wins
5. Refactors

# EXPLORATION

Explore the repo. Read `CONTEXT.md` (domain vocabulary — use its terms in code, tests, commits) and `CONVENTIONS.md` (implementation patterns) before writing code. Ignore `.archive/`.

# IMPLEMENTATION

Use /tdd to complete the task.

# FEEDBACK LOOPS

First, if `.venv` is missing or broken (it is never portable across machines/sandboxes), run `uv sync` and confirm `uv run pytest -q` is green before touching code.

Before committing, run the feedback loops:

- `uv run pytest -q` to run the tests (the only feedback loop this repo has; no mypy/ruff configured)

# COMMIT

Make a git commit. The commit message must:

1. Include key decisions made
2. Include files changed
3. Blockers or notes for next iteration

# THE ISSUE

If the task is complete, move the issue to done:

`gh issue edit <number> --remove-label ready-for-agent --add-label agent:done`

If the task is not complete, add a note to the issue with what was done:

`gh issue comment <number> --body "<what was done, what is left>"`

Leave the `ready-for-agent` label in place if the task is not complete.

# FINAL RULES

ONLY WORK ON A SINGLE TASK.
