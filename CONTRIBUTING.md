# Contributing Guide

Thank you for contributing to codexSync.

## Inbound = Outbound Licensing

By submitting a contribution (code, docs, tests, or other material), you agree that your contribution is licensed under `GPL-3.0-or-later` for the open-source version of this project.

## Contributor License Agreement (CLA)

To keep dual licensing possible (`GPL-3.0-or-later` + commercial licenses), contributors must also grant the maintainers the right to relicense their contributions.

Before your first contribution is accepted, you must agree to the terms in [CLA.md](./CLA.md).

## Contribution Workflow

1. Fork the repository.
2. Create a branch for your change.
3. Keep changes focused and documented.
4. Add or update tests when behavior changes.
5. Open a pull request.

## Documentation

- User documentation lives in `docs/en/`, with the same pages in Russian in `docs/ru/` and in Chinese in `docs/zh/`; a change to one language is a change to all three. `python -m pytest tests/test_docs_links.py` checks that the pages match and that every link resolves.
- Developer documents live in `docs/dev/`: [decisions](./docs/dev/DECISIONS.md), the [release checklist](./docs/dev/PUBLISHING.md), and the [controlled experiments](./docs/dev/experiments) that open the gates which are still closed.

## Test and Platform Notes

- Project packaging requires Python `3.11+` (see `pyproject.toml`).
- CI currently validates on `windows-latest` and `macos-latest`.
- Linux runtime support is currently out of MVP scope, so Linux CI is intentionally disabled.
- Markdown files use the lowercase `.md` extension. Document names stay uppercase (`README.md`, `AI_RULES.md`); only the extension is lowercase.

## Quality and Safety Expectations

Given the project scope (local state sync), contributions should preserve these safety properties:

- Never write state while Codex is running.
- Always create a backup before overwrite.
- Exclude temporary/lock/cache files.
- Fail safely when state is uncertain.

## Legal Notice

Do not submit code you do not have the right to contribute.
By submitting, you confirm you have the legal authority to license your contribution under the terms above.
