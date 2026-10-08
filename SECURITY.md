# Security policy

codexSync works with sensitive local state: your Codex chats, projects and
configuration, and the folder that carries them between your machines. A fault
that leaks that state, or that loses or silently replaces it, is treated as a
security problem, not as an ordinary bug.

## What counts

Report it privately if codexSync could:

- copy, upload or log credentials — `auth.json` and the other files it is
  meant never to carry — or the content of chats where it should not;
- write into `.codex` while Codex is running, or when it cannot tell whether
  Codex is running;
- replace or delete state without a verified backup first, or leave a backup
  that does not restore what it claims to;
- adopt a file that another machine, a cloud client or a person changed as if
  it were its own (a journal, a manifest, a plan, a handoff record);
- write outside the folders the configuration names;
- run something other than what an installed scheduled task was meant to run.

A crash, a wrong message or a refusal that leaves your state untouched is an
ordinary bug: open an [issue](https://github.com/kroxiksut/codexSync/issues).

## How to report

Use GitHub's private reporting:
[report a vulnerability](https://github.com/kroxiksut/codexSync/security/advisories/new).
Only the maintainer sees it. Describe what you did, what happened and what you
expected, and include the codexSync version (`codexsync --version`), your
operating system, and whether you used the window or the command line.

**Never attach real data** — not in a private report and not in a public
issue: no `.codex` folder or part of it, no `auth.json`, no session `.jsonl`
files, no `state_*.sqlite` databases, no `config.toml` with your paths, no
logs with chat titles. If a reproduction needs files, describe their shape or
make a small invented example.

After a fix is released, a confirmed vulnerability may be published as a
security advisory. We will credit the reporter unless they prefer to remain
anonymous.

## Supported versions

| Version | Fixes |
|---|---|
| 0.2.0 pre-releases (alphas, release candidates) and main | yes |
| 0.1.x | yes, until 0.2.0 is released |

<!-- At the 0.2.0 release: "0.2.x | yes" and "0.1.x | no — upgrade to 0.2". -->
