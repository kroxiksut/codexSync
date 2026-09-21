from __future__ import annotations

from pathlib import Path
import unittest

from codexsync.config import load_config, parse_config_text
from codexsync.process_knowledge import BACKGROUND_PROCESS_NAMES, OS_KEYS, PROCESS_NAMES
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


    def test_template_process_lists_match_process_knowledge(self) -> None:
        """The template may not know more -- or less -- than the code does.

        Before CS-256 the 0.2 process names lived only in this template while
        `config.py` still defaulted to the 0.1 lists, so a config without a
        `[process_detection]` section detected less than the version could and
        said nothing about it.
        """
        cfg = load_config(PACKAGED_TEMPLATE)
        self.assertEqual(tuple(cfg.process_detection.process_names), PROCESS_NAMES)
        for os_key in OS_KEYS:
            self.assertEqual(
                tuple(cfg.process_detection.background_process_names[os_key]),
                BACKGROUND_PROCESS_NAMES[os_key],
                f"{os_key} markers in the template differ from process_knowledge",
            )

    def test_absent_process_detection_falls_back_to_the_same_lists(self) -> None:
        """A config with no `[process_detection]` must detect what we know."""
        text = PACKAGED_TEMPLATE.read_text(encoding="utf-8")
        kept: list[str] = []
        skipping = False
        for line in text.splitlines():
            if line.startswith("[process_detection"):
                skipping = True
            elif line.startswith("[") and skipping:
                skipping = False
            if not skipping:
                kept.append(line)
        cfg = parse_config_text(
            chr(10).join(kept), base_dir=PACKAGED_TEMPLATE.parent, source="<stripped template>"
        )
        self.assertEqual(tuple(cfg.process_detection.process_names), PROCESS_NAMES)
        for os_key in OS_KEYS:
            self.assertEqual(
                tuple(cfg.process_detection.background_process_names[os_key]),
                BACKGROUND_PROCESS_NAMES[os_key],
            )


if __name__ == "__main__":
    unittest.main()
