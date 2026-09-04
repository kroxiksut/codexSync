from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.config import load_config
from codexsync.runtime import _require_mutation_compatible_config


REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_TEMPLATE = REPO_ROOT / "config.example.toml"
PACKAGED_TEMPLATE = REPO_ROOT / "src" / "codexsync" / "config.example.toml"


class ConfigTemplateParityTests(unittest.TestCase):
    """The repo-root template is a copy; only the packaged one ships.

    Without this guard the two files drift silently and users who run
    `init-config` get a different template than the one in the repository.
    """

    def test_root_template_matches_packaged_template(self) -> None:
        self.assertTrue(ROOT_TEMPLATE.exists(), f"missing {ROOT_TEMPLATE}")
        self.assertTrue(PACKAGED_TEMPLATE.exists(), f"missing {PACKAGED_TEMPLATE}")
        self.assertEqual(
            PACKAGED_TEMPLATE.read_bytes(),
            ROOT_TEMPLATE.read_bytes(),
            "config.example.toml at the repo root drifted from src/codexsync/config.example.toml",
        )

    def test_packaged_template_passes_config_validation(self) -> None:
        # The shipped template must survive the same validation real configs get.
        load_config(PACKAGED_TEMPLATE)

    def test_packaged_template_is_usable_for_mutations(self) -> None:
        """`init-config` must not hand the user a config that cannot sync.

        Every mutation command runs this check, so a template that fails it
        makes sync/restore/repair/recover exit 4 straight out of the box.
        """
        _require_mutation_compatible_config(load_config(PACKAGED_TEMPLATE))


if __name__ == "__main__":
    unittest.main()
