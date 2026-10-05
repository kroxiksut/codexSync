from __future__ import annotations

import unittest
from unittest import mock

from codexsync.process_detector import PROVEN_DETECTORS, CodexProcessDetector
from codexsync.ubuntu_support import UbuntuRuntime, inspect_ubuntu_runtime


class UbuntuRuntimeTests(unittest.TestCase):
    def test_supported_maintained_releases(self) -> None:
        for version in ("24.04", "26.04"):
            with self.subTest(version=version):
                runtime = inspect_ubuntu_runtime({"ID": "ubuntu", "VERSION_ID": version})
                self.assertTrue(runtime.supported)
                self.assertEqual(runtime.detector_key, f"ubuntu:{version}")

    def test_other_ubuntu_release_is_outside_support_scope(self) -> None:
        runtime = inspect_ubuntu_runtime({"ID": "ubuntu", "VERSION_ID": "25.10"})
        self.assertFalse(runtime.supported)
        self.assertIsNone(runtime.detector_key)
        self.assertIn("24.04, 26.04", runtime.detail)

    def test_other_linux_distribution_is_outside_support_scope(self) -> None:
        runtime = inspect_ubuntu_runtime({"ID": "fedora", "VERSION_ID": "43"})
        self.assertFalse(runtime.supported)
        self.assertIsNone(runtime.detector_key)
        self.assertIn("Ubuntu only", runtime.detail)


class UbuntuCapabilityTests(unittest.TestCase):
    def test_supported_release_stays_closed_without_runtime_proof(self) -> None:
        runtime = UbuntuRuntime("26.04", True, "Ubuntu 26.04")
        with mock.patch("codexsync.process_detector.sys.platform", "linux"), \
                mock.patch("codexsync.process_detector.current_ubuntu_runtime", return_value=runtime):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertFalse(capability.supported)
        self.assertIn("not been run against a live Codex", capability.detail)

    def test_proof_is_scoped_to_the_observed_ubuntu_release(self) -> None:
        runtime = UbuntuRuntime("26.04", True, "Ubuntu 26.04")
        with mock.patch("codexsync.process_detector.sys.platform", "linux"), \
                mock.patch("codexsync.process_detector.current_ubuntu_runtime", return_value=runtime), \
                mock.patch.dict(
                    PROVEN_DETECTORS,
                    {"ubuntu:26.04": "Ubuntu 26.04, ChatGPT/Codex observed"},
                    clear=False,
                ):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertTrue(capability.supported)
        self.assertEqual(capability.platform, "Ubuntu 26.04")

    def test_proof_for_one_ubuntu_release_does_not_open_another(self) -> None:
        runtime = UbuntuRuntime("24.04", True, "Ubuntu 24.04")
        with mock.patch("codexsync.process_detector.sys.platform", "linux"), \
                mock.patch("codexsync.process_detector.current_ubuntu_runtime", return_value=runtime), \
                mock.patch.dict(
                    PROVEN_DETECTORS,
                    {"ubuntu:26.04": "Ubuntu 26.04, ChatGPT/Codex observed"},
                    clear=False,
                ):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertFalse(capability.supported)

    def test_unsupported_linux_distribution_stays_closed_even_with_linux_proof(self) -> None:
        runtime = UbuntuRuntime("43", False, "Linux distribution 'fedora' is outside support scope")
        with mock.patch("codexsync.process_detector.sys.platform", "linux"), \
                mock.patch("codexsync.process_detector.current_ubuntu_runtime", return_value=runtime), \
                mock.patch.dict(PROVEN_DETECTORS, {"linux": "generic proof"}, clear=False):
            capability = CodexProcessDetector(["codex"]).capability()
        self.assertFalse(capability.supported)


if __name__ == "__main__":
    unittest.main()
