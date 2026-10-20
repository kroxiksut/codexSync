#!/usr/bin/env node
"use strict";
// The window. On Windows, started with no command it is left running on its
// own; with a command (`codexsync-gui sync ...`) it is waited for like the
// command line, and so is the window on Linux, where a missing system library
// is reported in the terminal that started it.
const detach = process.platform === "win32" && process.argv.length <= 2;
require("../lib/launch.js").run("codexsync-gui", { detach });
