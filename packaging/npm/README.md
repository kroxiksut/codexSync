# codexSync

Hand your Codex work over between machines through a cloud folder you choose:
settings, chats, chat names and the project list, with a verified backup before
every overwrite and a journal that can resume or roll back an interrupted write.
codexSync writes only while Codex is closed and never copies `auth.json`.

```sh
npm install -g codexsync
codexsync-gui                        # the window; on first start it helps you create config.toml
codexsync init-config --output config.toml
codexsync -c config.toml doctor      # read-only: what is where, and whether Codex is open
```

This package is a launcher. The programs are the release builds, installed as
the optional package for your system: Windows x64 and ARM64, Linux x64 and
ARM64 (glibc 2.35 or later on x64, 2.39 on ARM64). There is no macOS build on
npm; there, use `pipx install codexsync`. Nothing runs at install time.

Install it globally rather than through `npx`: a scheduled task codexSync sets
up names the program where it is installed, and `npx` keeps it in a cache that
is cleared. On Windows a running program cannot be replaced: before
`npm update -g codexsync`, close the window and end the handoff watcher if it
runs (Task Scheduler, codexSync's task, *End*); it starts again at the next
sign-in.

- [Documentation](https://github.com/kroxiksut/codexSync/blob/main/docs/en/README.md)
- [Changelog](https://github.com/kroxiksut/codexSync/blob/main/CHANGELOG.md)
- [Issues](https://github.com/kroxiksut/codexSync/issues)

codexSync is not affiliated with OpenAI; Codex is OpenAI's. Licence:
GPL-3.0-or-later.
