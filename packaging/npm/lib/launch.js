"use strict";
// Starts the codexSync program for this system (CS-416).
//
// `codexsync` on npm is only this launcher. The programs are the release
// builds, one package per system, which npm installs as an optional
// dependency where `os` and `cpu` match -- the way Codex's own npm package is
// laid out. Nothing is downloaded, built or run at install time.
//
// The launcher passes the arguments through and returns the program's exit
// code, so `codexsync` from npm behaves like the program itself: 0 done,
// 2 conflict, 3 Codex open, 4 config, 5 fail-safe. Exit 4 is also what it
// returns when it cannot start the program at all, as a missing window toolkit
// does in the Python package.

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

// The systems a build is published for -- those Codex itself is published for,
// less macOS, where codexSync has no frozen build. Kept equal to `PLATFORMS`
// in scripts/npm_packages.py by tests/test_npm_packages.py.
const PLATFORMS = ["win32-x64", "win32-arm64", "linux-x64", "linux-arm64"];

const CANNOT_START = 4;

class LaunchError extends Error {}

function platformKey(platform = process.platform, arch = process.arch) {
  return `${platform}-${arch}`;
}

function programPath(program, { platform = process.platform, arch = process.arch, resolve = require.resolve } = {}) {
  const key = platformKey(platform, arch);
  if (!PLATFORMS.includes(key)) {
    throw new LaunchError(
      `there is no codexSync build for ${key} on npm. With Python 3.11 or later: ` +
        "pipx install codexsync (https://github.com/kroxiksut/codexSync#install)",
    );
  }
  let manifest;
  try {
    manifest = resolve(`codexsync-${key}/package.json`);
  } catch {
    throw new LaunchError(
      `the program for ${key} is not installed (npm skipped the optional package codexsync-${key}). ` +
        "Install again without --omit=optional: npm install -g codexsync",
    );
  }
  return path.join(path.dirname(manifest), "dist", platform === "win32" ? `${program}.exe` : program);
}

// npm keeps an executable bit when it packs a file, but a copy made by other
// means may not have one. Setting it is all that is done; a folder this user
// cannot change is reported by the start itself.
function ensureExecutable(file) {
  if (process.platform === "win32") return;
  try {
    fs.accessSync(file, fs.constants.X_OK);
  } catch {
    try {
      fs.chmodSync(file, 0o755);
    } catch {
      // the spawn below says why it cannot run
    }
  }
}

function fail(message) {
  console.error(`codexsync: ${message}`);
  process.exit(CANNOT_START);
}

function run(program, { detach = false } = {}) {
  let file;
  try {
    file = programPath(program);
  } catch (error) {
    if (error instanceof LaunchError) fail(error.message);
    throw error;
  }
  if (!fs.existsSync(file)) fail(`${file} is missing; install codexsync again`);
  ensureExecutable(file);
  const args = process.argv.slice(2);

  if (detach) {
    // The window on Windows: it has no console to share, and a launcher left
    // waiting would keep a console window open beside it.
    const child = spawn(file, args, { detached: true, stdio: "ignore" });
    child.on("error", (error) => fail(`cannot start ${file}: ${error.message}`));
    child.unref();
    return;
  }

  const child = spawn(file, args, { stdio: "inherit" });
  // Ctrl+C reaches the program from the terminal itself; the launcher only
  // stays alive to report how it ended. A signal sent to the launcher alone
  // is passed on -- but never on Windows, where `kill` would end the program
  // at once instead of letting it finish or roll back its write.
  process.on("SIGINT", () => {});
  if (process.platform !== "win32") {
    for (const signal of ["SIGTERM", "SIGHUP"]) {
      process.on(signal, () => child.kill(signal));
    }
  }
  child.on("error", (error) => fail(`cannot start ${file}: ${error.message}`));
  child.on("exit", (code, signal) => {
    if (signal) {
      process.removeAllListeners(signal);
      process.kill(process.pid, signal);
      return;
    }
    process.exit(code === null ? 1 : code);
  });
}

module.exports = { PLATFORMS, LaunchError, platformKey, programPath, run };
