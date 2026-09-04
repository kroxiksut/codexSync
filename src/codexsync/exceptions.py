class CodexSyncError(Exception):
    """Base error for codexsync."""


class ConfigError(CodexSyncError):
    """Invalid or incomplete configuration."""


class SafetyPreconditionError(CodexSyncError):
    """Safety rule violation."""


class ConflictError(CodexSyncError):
    """Conflict that requires manual resolution."""


class FailSafeError(CodexSyncError):
    """Safe stop due to uncertainty."""


class GuardianIntegrityError(FailSafeError):
    """Guardian snapshot or manifest cannot be trusted."""


class GuardianBusyError(FailSafeError):
    """Another Guardian writer already holds the per-machine lock."""


class OperationBusyError(FailSafeError):
    """Another mutation operation owns the same local Codex state root."""
