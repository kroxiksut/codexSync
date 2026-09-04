from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch
import unittest

from codexsync.cli import main
from codexsync.exceptions import ConfigError


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(sync=SimpleNamespace(dry_run_default=True)),
        plan=SimpleNamespace(conflicts=[]),
    )


class CliManualConfirmationTests(unittest.TestCase):
    @patch("codexsync.cli.run_sync")
    @patch("codexsync.cli.build_context", return_value=_ctx())
    @patch("codexsync.cli.load_config", side_effect=ConfigError("skip verbose config load"))
    def test_sync_without_flag_passes_none_override(self, _load_cfg, build_ctx, _run_sync) -> None:
        code = main(["-c", "config.toml", "sync", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(build_ctx.call_args.kwargs["manual_terminate_confirmation_override"], None)

    def test_manual_terminate_flag_is_rejected(self) -> None:
        code = main(["-c", "config.toml", "--manual-terminate-confirmation", "sync", "--dry-run"])
        self.assertEqual(code, 4)

    def test_auto_terminate_flag_is_rejected(self) -> None:
        code = main(["-c", "config.toml", "--auto-terminate-without-confirmation", "sync", "--dry-run"])
        self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
