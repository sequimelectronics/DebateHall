# Contributing to Debate Hall

Thank you for helping. A few things to know first.

## License of contributions

Debate Hall is licensed **AGPL-3.0-only** (see `LICENSE`), like its sister project AI-Beach. By contributing, you
agree that your contribution is licensed AGPL-3.0-only too. Art you contribute is licensed **CC BY-SA 4.0**.

Every source file starts with these two lines, in its own comment style:

```text
SPDX-License-Identifier: AGPL-3.0-only
Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
```

`tests/test_spdx.py` fails if a source file is missing them.

## The household rules

This repository grew up in a household, and its rules still hold:

- **No real people's or households' names or details** anywhere: in code, tests, docs, screenshots or commit
  history. Use neutral test names (Sandy, Pebble, Coral, Drift, Shell, Kelp) and they/them for anyone unnamed.
  History is permanent, so check before you commit. The only name is the copyright line in the headers.
- **The doohicky rule:** in anything an agent reads (seat briefs, error messages, the helper's help), examples use
  obviously made-up names and things ("Person A says the doohicky study overclaims"), never a name that could be a
  real person or a real paper. A model copies an example as it stands, so an example has to be impossible to
  mistake for a real record.
- **Credit by role, not name:** a comment that records where an idea came from says "(the keeper, 10-25)", "(an
  advocate, a test debate)" or "(a security review)", never a person's or an agent's name.
- The person who runs a Hall is the **keeper**, with **admin** rights. The **judge** is a role in a debate, not the
  keeper (one person may be both).
- **Nothing from the internet at runtime**, apart from the local machine. No accounts, no telemetry.
- **Readable first**, American spelling (`tests/test_spelling.py` checks it), and the phone width works.
- Every subprocess gets explicit stdin, stdout and stderr (Windows matters).

## Before you send a change

```bash
python -m unittest discover -s tests
```

Add a line to [CHANGELOG.md](CHANGELOG.md) under today's date, starting with what kind of change it is:
**Security**, **Feature**, **Fix**, **Mod** or **Docs**. Keep it to a sentence or two; say "Security" whenever a change
fixes a weakness or updates a dependency with published advisories, so keepers know to update.
