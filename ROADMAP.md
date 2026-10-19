# Roadmap

English · [Русский](./ROADMAP.ru.md) · [Project page](./README.md) · [Changelog](./CHANGELOG.md)

What comes next, and in what order. This is a plan, not a promise: an item
moves, changes or is dropped when what we learn says so. Shipped work goes to
the [changelog](./CHANGELOG.md).

**Rule: until 0.2.0 is released, work goes only into 0.2.** Nothing under 0.3
is started before then, not even a prototype.

## 0.2 — now

0.2.0 alpha 1 is out. Before 0.2.0 itself:

- reports from more machines than ours — please
  [tell us](https://github.com/kroxiksut/codexSync/issues/new/choose) what
  happened on yours;
- writes on macOS, which stay closed until someone checks the process detection
  on a Mac against a real Codex (see [Have a Mac?](./README.md#have-a-mac));
- fixes for what the alpha reports turn up.

The scope of 0.2 is fixed: no new features go in.

## 0.3 — questions to research

Each item here starts as a question. It becomes work only after a controlled
experiment on disposable data answers it, and a recorded decision says how.

### A ChatGPT conversation as context for a Codex project

**The problem.** The ChatGPT and Codex histories are separate, even in the one
desktop app. A design worked out in ChatGPT — the UX discussion, the variants,
generated mock-ups — does not count against Codex limits, but nothing carries
it into Codex: the work has to be explained again from scratch.

**The idea.** Not to turn a ChatGPT conversation into a Codex chat, but to put a
*context bundle* beside a chosen project, which Codex is told to read:

```text
<project>/.codexsync/context/<conversation>/
  handoff.md      short brief: goal, decisions, requirements, assets, rejected options, open questions
  transcript.md   the whole conversation, with roles
  manifest.json   where it came from and when
  assets/         images generated or attached in the conversation
```

Codex reads `handoff.md` first, opens `transcript.md` only for the reasoning
behind a decision, and looks at the images before touching the UI.

**What it will never do:** create a Codex chat or a row in Codex's database,
call ChatGPT's internal endpoints, or read a sign-in token or cookie. The same
rules as the rest of codexSync.

**Open questions:**

1. **Where the conversation comes from.** First, an experiment: does the
   desktop app keep the whole conversation locally — text, order, roles,
   images — in a shape that can be read without touching sign-in data? The
   first step lists folder names and sizes only. If all it keeps is a web cache
   and server ids, that path is dropped. The fallback is the official data
   export (`conversations.json` and the images): slow and whole-account, but
   documented and offline. A conversation there is a tree (each edited message
   starts a branch), so the transcript follows the branch that was shown last.
   A shared link is not the main path: it publishes a snapshot just to hand it
   to a local program, and leaves out the files of a ChatGPT project.
2. **Who writes `handoff.md`.** codexSync has no model and will not get one.
   The brief is written either by ChatGPT at the end of the conversation (free
   for Codex limits) or by Codex from the transcript. codexSync places the
   files; the summary is a model's job.
3. **Writing into a project folder.** codexSync reads project folders today and
   never writes into them. Where the bundle goes, whether it is added to
   `.gitignore`, and whether `AGENTS.md` is edited or only a line to paste is
   offered — each is a decision for the owner.
4. **Updates.** When the conversation continues, add only the new messages and
   images to an existing bundle.

**Until then,** the manual version gives most of the value: at the end of the
conversation ask ChatGPT for a `handoff.md` in the shape above, download the
images, put both in the project and tell Codex to read them.
