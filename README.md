# BlackSwan — engineering operating standard

**Status:** Project orientation and standards, 23 November 2026. **Not** an implementation, migration plan, authorisation to trade, or claim that any revision is live-verified.

## Outcome

Build a *profitable, fast, modular, measurable* Robinhood Chain (chain ID 4663) trading system seeking positively skewed / extreme right-tail opportunities. Evaluate **net realised GBP outcome** and the ability to capture executable upside, not aesthetics, inflated paper multiples, number of tests, or mere completion of code.

The engineering partner acts as a capable, questioning **engineer, programmer, researcher, and statistician**. It is expected to devise and evaluate solutions, challenge weak assumptions, and do serious independent technical work. The user establishes investment objectives, acceptable exposure, overall strategy and material scope; technical discretion within an agreed task is expected. Intellectual ownership, deference, lengthy process and reassurance are **not** success metrics.

## Orientation only — future desired baseline

The **user reports** that the original detector and earlier peak-tracking/execution system, coupled to the **buying-block filter**, worked for over one week. Treat that specific implementation as the **candidate proven baseline**, to be identified by its actual commit, run evidence, and dependencies **before modification**. Do **not** assume any newer modular rewrite is the baseline.

Future architecture (no implementation authorised by this pack):

1. Working detector remains independent.
2. Selector interface makes **buying-block** and **buy-concentration** selectors interchangeable without changing execution.
3. Execution owns the **sacrifice**, real buy, signing, receipt reconciliation, and sale process. Selection must not own sacrifice execution.
4. Preserve the original flexible **campaign size, stake value, and peak/exit behaviour** unless explicitly changed.
5. Reuse the effective earlier monitor as a reference, with real-time operational and financial evidence.

See `docs/DIRECTION_AND_BOUNDARIES.md` for the precise intended boundaries and what is still unknown. **No trading code or live campaign has been changed by this pack.**

## Put this pack in a new folder

For a new *working repository*, copy the **contents** of this folder into its root. `AGENTS.md` must be at the root, alongside eventual source code. Keep `docs/`, `templates/`, `tools/`, and `tests/` as shown.

For a new **AI Project/workspace** (ChatGPT, Claude, or similar), upload `AGENTS.md`, `README.md`, and the files in `docs/` as Project files. Paste the instructions from `PROJECT_INSTRUCTIONS.md` into that Project's custom instructions. For **Claude Code**, keep `CLAUDE.md` at the repository root as well. Keep the complete pack in the actual engineering repository too: a Project upload is not an executable quality gate.

If starting with the old source in another repository, **do not overwrite its root or its existing `AGENTS.md` blindly**. First identify the original working baseline, compare standards and merge deliberately. No source files in this pack.

## Pack contents

- `PROJECT_INSTRUCTIONS.md` — concise text for ChatGPT Project instructions.
- `AGENTS.md` — binding engineering behaviour, priorities, hard safety rules and verification standard.
- `CLAUDE.md` — Claude Code entry point referencing the same operating standard.
- `docs/DIRECTION_AND_BOUNDARIES.md` — strategy orientation, candidate baseline, selector/execution boundaries.
- `docs/ENGINEERING_STANDARD.md` — architecture, debugging, performance, code and observability.
- `docs/STATISTICAL_STANDARD.md` — Taleb-inspired empirical approach and honest outcomes.
- `docs/OPERATIONAL_SAFETY.md` — terminals, wallets, RPC and live-money boundaries.
- `docs/RELEASE_GATES.md` — objective evidence gates and mandatory release report.
- `docs/DECISIONS_AND_BASELINE.md` — initial settled direction and baseline evidence checklist.
- `templates/CHANGE_REPORT.md` — concise evidence-driven change report template.
- `templates/ADVERSARIAL_REVIEW.md` — ready-to-use independent red-team review prompt.
- `tools/check_terminal_commands.py` — offline scanner for Markdown shell command blocks intended for users.
- `tests/test_terminal_commands.py` — automated tests for that scanner.

## First steps in the future new project

**Do not start rewriting.** First identify the earlier working source, freeze its commit and historical campaign configurations, and collect evidence of detector → selector → sacrifice/buy → peak tracking → sale → monitor behaviour. Then run baseline tests and reproduce the historical functionality. Only then plan the selector boundary work. This is orientation, not permission to act now.

## Minimal standards-check command (run inside the unpacked folder)

```bash
python3 -m unittest discover -s tests -v
```

`tools/check_terminal_commands.py` accepts the Markdown file containing proposed user-facing shell commands. Example: `python3 tools/check_terminal_commands.py proposed_commands.md`. It catches shell-terminating constructs, but cannot prove arbitrary shell commands are safe: code review remains essential.
