class CodexSyncError(Exception):
    """Base error for codexsync."""


class ConfigError(CodexSyncError):
    """Invalid or incomplete configuration."""


class ConfigOutdatedError(ConfigError):
    """A config written by an earlier version sets a value this one refuses.

    Still exit 4: to the command line it is a configuration error like any
    other. The subclass exists so a window can say it in its own language and
    offer the upgrade (`config upgrade`) instead of quoting the English text.
    """

    code = "CONFIG_OUTDATED"

    def __init__(self, message: str, *, setting: str) -> None:
        super().__init__(message)
        #: The dotted key that makes the config unusable, e.g. ``sync.session_mode``.
        self.setting = setting


class SafetyPreconditionError(CodexSyncError):
    """Safety rule violation."""


class ConflictError(CodexSyncError):
    """Conflict that requires manual resolution."""


class ChatDecisionsNeeded(ConflictError):
    """A full sync found chats only a person can decide; nothing was written.

    Still exit 2. The counts and the machine pair exist so a window can say
    what kind of decisions wait, in its own language, and open the page where
    they are made for exactly that pair (CS-342).
    """

    code = "CHAT_DECISIONS_NEEDED"

    def __init__(
        self, *, source: str, target: str, format_migrations: int = 0, held_migrations: int = 0,
        divergences: int = 0, collisions: int = 0,
    ) -> None:
        self.source = source
        self.target = target
        self.format_migrations = format_migrations
        self.held_migrations = held_migrations
        self.divergences = divergences
        self.collisions = collisions
        total = format_migrations + held_migrations + divergences + collisions
        super().__init__(
            f"{total} chat(s) need a decision; nothing was written. Decide them all by one rule for "
            f"this run with `handoff sync --conflict-policy prefer_newer_mtime` (or prefer_local, "
            f"prefer_cloud), or for every run with `[conflict] policy`; the copy not kept is saved "
            f"whole in the conflict bundle. To decide one by one: `sessions scan --source-machine "
            f"{source} --target-machine {target}`"
        )

    @property
    def details(self) -> dict[str, object]:
        return {
            "source": self.source, "target": self.target,
            "format_migrations": self.format_migrations, "held_migrations": self.held_migrations,
            "divergences": self.divergences, "collisions": self.collisions,
        }


class FailSafeError(CodexSyncError):
    """Safe stop due to uncertainty."""


class GuardianIntegrityError(FailSafeError):
    """Guardian snapshot or manifest cannot be trusted."""


class GuardianBusyError(FailSafeError):
    """Another Guardian writer already holds the per-machine lock."""


class OperationBusyError(FailSafeError):
    """Another mutation operation owns the same local Codex state root."""


class RecoveryPendingError(FailSafeError):
    """An earlier operation's journal is still open, so no mutation may start.

    Still exit 5. The subclass exists so a window can say it in its own
    language and open its recovery page on that journal, instead of quoting
    the English text. ``begin`` raises it only after closing this machine's
    journals that provably replaced nothing, so what is left needs a person.
    """

    code = "RECOVERY_PENDING"

    def __init__(
        self, message: str, *, operation_id: str, family: str, state: str, machine: str | None,
    ) -> None:
        super().__init__(message)
        self.operation_id = operation_id
        self.family = family
        self.state = state
        self.machine = machine

    @property
    def details(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id, "family": self.family,
            "state": self.state, "machine": self.machine,
        }
