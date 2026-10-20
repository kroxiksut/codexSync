# Experiment: writing a chat Codex keeps compressed

## Status

**Run 2026-10-19** on the test laptop (Ubuntu 26.04, Codex desktop 26.1013.40934,
bundled codex 0.163.0-alpha.10.2), without looking at the screen: each chat was
opened through Codex's own app-server over stdio (`initialize`,
`thread/resume`, `thread/unsubscribe`; no turn, no model request) and its
turns, the file and `thread_history_projection_state` (read from a copy) were
recorded. Results below. C is acted on (`IN_PLACE_PROJECTED_REWRITE`), and so
is B since 2026-10-20 (CS-430, D-019 amendment): a compressed chat the other
machine continued is written as plain JSONL and the `.zst` removed after its
backup (`IN_PLACE_DECOMPRESSED`); a resolved conflict over one still waits in
the mirror (`IN_PLACE_CONTAINER`). Still owed: one end-to-end run on the
laptop -- `sessions apply` over a compressed chat, then `thread/resume` shows
the new turn and the `.zst` is in the backup.

## Why

Codex 0.163 (first seen 2026-10-15) keeps every chat it has not touched for a
while as `rollout-….jsonl.zst`, compressed with zstd, while `threads.rollout_path`
in `state_<n>.sqlite` still names the plain `rollout-….jsonl`. codexSync reads
those files on Python 3.14 and later. It does not write them: `.codex` is
written plain only. That leaves two questions this experiment answers.

1. **Plain beside compressed.** What does Codex read when `x.jsonl` and
   `x.jsonl.zst` both exist? If it reads the plain file, a continuation from
   the other machine could be written as `x.jsonl` with the `.zst` removed
   through the plan's deletions (backed up and verified first, the way an
   archive move already removes its old file, `D-023`). If it reads the `.zst`,
   a plain file beside it is a copy nobody reads.
2. **Codex's own history store.** Codex 0.163 also keeps a projection of each
   chat in `thread_history_1.sqlite`. `thread_history_projection_state` holds,
   per thread, the byte offset and record ordinal in the rollout file it has
   read up to (`next_rollout_byte_offset`, `next_rollout_ordinal`). An in-place
   fast-forward (`D-019`) only appends, so that offset still points at the same
   bytes. Replacing a file's content, which is what resolving a conflict for the
   other machine's branch does, does not keep it there. Whether Codex notices,
   rebuilds or shows a broken chat is unknown.

## Where to run it

On the disposable test machine only (the laptop), with a Codex that compresses
chats (0.163 or later). Never on the machine that holds the real chats. Close
Codex before every file change and open it after. Each step changes a chat's
files by hand, which is the point. Make a copy of `.codex` first
(`codexsync state-backup create`, or copy the folder).

## Steps

Pick a compressed chat with at least two turns whose last turn you can
recognise: `sessions/…/rollout-…-<id>.jsonl.zst`. Make a **shorter plain
copy**: decompress it (`python3.14 -c "from compression import zstd;
import sys; sys.stdout.buffer.write(zstd.decompress(open(sys.argv[1],'rb').read()))" x.jsonl.zst > full.jsonl`),
then drop the records of the last turn (everything after the last
`turn_context` record before it). The last turn now tells the two files apart.

| # | Files | Look at |
|---|---|---|
| A | `x.jsonl.zst` and the shorter `x.jsonl` beside it | Is the last turn shown? (shown means Codex read the `.zst`). After Codex closes: are both files still there, and is either rewritten or recompressed? |
| B | Only the shorter `x.jsonl` (the `.zst` moved out of `.codex`) | Is the chat listed and does it open, without the last turn? Does Codex compress it again later? |
| C | Restore the `.zst`. With Codex closed, replace it with a zstd file of a *different* history of the same chat: the shorter copy plus one turn of another chat's records | Does the chat open? Which turns? Anything in Codex's log about `thread_history`? Does `thread_history_projection_state` for the thread change after Codex has started (read it with `immutable=1`)? |

Put the copy of `.codex` back afterwards.

## Record

| Field | Value |
|---|---|
| Date, Codex version (`models_cache.json` `client_version`), OS | 2026-10-19, 0.163.0, Ubuntu 26.04. Online for the first time, Codex compressed every chat at once (309 of 309), not only old ones |
| Opening a compressed chat | It is written back as plain `x.jsonl` (the `.zst` is gone) with one `event_msg` `thread_settings_applied` appended; a projection is built at the plain file's end offset (CS-251: opening writes) |
| A: which file Codex read; files after | The plain one: projection ordinal 86 = the plain file's records (the `.zst` has 195); the `.zst` untouched, the plain file got the appended record |
| B: listed / opens / recompressed | Listed and opens, read from the plain file (ordinal 51); recompression not seen in the minutes watched |
| C: opens / which turns / projection offset before and after | Opens, but shows the *old* four turns from the projection; offset stays 115115 while the replaced file is 110418 bytes. Broken silently |
| Append after a projection (fast-forward) | Shown: five turns, offset moved to the new end (149088) |

## What the results unlock

- **B works** (Codex reads a plain file that replaced the `.zst`): a chat that
  grew on the other machine can be written as plain JSONL, with the `.zst`
  removed through `SyncPlan.deletions`, under a code of its own. This would be
  a decision recorded in `docs/dev/DECISIONS.md`.
- **A reads the plain file**: the same works without the deletion. That is
  still not a reason to leave two files behind.
- **C breaks the chat**: an in-place write that does not only append (a
  resolved conflict, a format migration) must be refused on a machine with
  `thread_history_*.sqlite` until Codex's handling of it is understood. A pure
  append is a separate observation: check that a fast-forwarded chat shows its
  new turns, in the third table of `session-layout-adapter.md`.
