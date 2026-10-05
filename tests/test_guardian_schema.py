from __future__ import annotations

import json
import unittest

from codexsync.guardian_models import ValidationStatus
from codexsync.guardian_schema import (
    BROKEN_BINDING_REFERENCE,
    BROKEN_ORDER_REFERENCE,
    BROKEN_PROJECT_REFERENCE,
    DUPLICATE_ORDER_REFERENCE,
    ROOT_PATHS_STRING_BINDINGS_SCHEMA,
    UNKNOWN_SCHEMA,
    build_binding_value,
    supports_project_creation,
    supports_root_remap,
    validate_global_state_references,
)


class GuardianSchemaTests(unittest.TestCase):
    def test_consistent_legacy_state_and_extra_fields_pass(self) -> None:
        state = self._state()
        state["future-ui-setting"] = {"enabled": True}

        report = validate_global_state_references(self._payload(state))

        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.project_count, 2)
        self.assertEqual(report.binding_count, 2)

    def test_dangling_order_missing_project_and_duplicate_order_are_rejected(self) -> None:
        cases = []
        dangling_order = self._state()
        dangling_order["project-order"] = ["project-a", "project-missing"]
        cases.append((dangling_order, BROKEN_ORDER_REFERENCE))
        incomplete_order = self._state()
        incomplete_order["project-order"] = ["project-a"]
        cases.append((incomplete_order, BROKEN_PROJECT_REFERENCE))
        duplicate_order = self._state()
        duplicate_order["project-order"] = ["project-a", "project-a"]
        cases.append((duplicate_order, DUPLICATE_ORDER_REFERENCE))

        for state, code in cases:
            with self.subTest(code=code):
                report = validate_global_state_references(self._payload(state))
                self.assertEqual(report.status, ValidationStatus.INVALID)
                self.assertIn(code, report.codes)

    def test_explicit_dangling_binding_is_rejected_but_ambiguous_binding_is_indeterminate(self) -> None:
        explicit = self._state()
        explicit["thread-project-assignments"]["thread-c"] = {
            "namespace": "legacy",
            "project_id": "project-missing",
        }
        report = validate_global_state_references(self._payload(explicit))
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertIn(BROKEN_BINDING_REFERENCE, report.codes)

        ambiguous = self._state()
        ambiguous["thread-project-assignments"]["thread-c"] = "not-a-recognized-id"
        report = validate_global_state_references(self._payload(ambiguous))
        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertEqual(report.codes, (UNKNOWN_SCHEMA,))

    def test_rootpaths_projects_with_string_bindings_are_recognised_read_only(self) -> None:
        state = {
            "local-projects": {
                "project-a": {
                    "id": "project-a",
                    "name": "Alpha",
                    "rootPaths": ["D:/alpha"],
                    "createdAt": 1,
                    "updatedAt": 2,
                },
                "project-b": {
                    "id": "project-b",
                    "name": "Beta",
                    "rootPaths": ["D:/beta"],
                    "createdAt": 3,
                    "updatedAt": 4,
                },
            },
            "project-order": ["project-a", "project-b"],
            "project-id-migrations": {},
            "thread-project-assignments": {
                "thread-a": "project-a",
                "thread-b": "project-b",
            },
        }

        report = validate_global_state_references(self._payload(state))

        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, ROOT_PATHS_STRING_BINDINGS_SCHEMA)
        self.assertEqual(report.project_count, 2)
        self.assertEqual(report.binding_count, 2)
        self.assertFalse(supports_root_remap(report.schema_id))
        self.assertFalse(supports_project_creation(report.schema_id))
        with self.assertRaises(ValueError):
            build_binding_value(report.schema_id, "project-a")

    def test_rootpaths_string_binding_to_missing_project_is_invalid_not_unknown(self) -> None:
        state = {
            "local-projects": {
                "project-a": {
                    "id": "project-a",
                    "name": "Alpha",
                    "rootPaths": ["D:/alpha"],
                    "createdAt": 1,
                    "updatedAt": 2,
                },
            },
            "project-order": ["project-a"],
            "project-id-migrations": {},
            "thread-project-assignments": {
                "thread-a": "project-missing",
            },
        }

        report = validate_global_state_references(self._payload(state))

        self.assertEqual(report.schema_id, ROOT_PATHS_STRING_BINDINGS_SCHEMA)
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertIn(BROKEN_BINDING_REFERENCE, report.codes)

    def test_empty_supported_state_is_valid_and_unknown_schema_is_not_empty_state(self) -> None:
        empty = {
            "local-projects": {},
            "project-order": [],
            "thread-project-assignments": {},
        }
        report = validate_global_state_references(self._payload(empty))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual((report.project_count, report.binding_count), (0, 0))

        report = validate_global_state_references(self._payload({"projects": []}))
        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertEqual(report.codes, (UNKNOWN_SCHEMA,))

    @staticmethod
    def _state() -> dict:
        return {
            "local-projects": {"project-a": {}, "project-b": {}},
            "project-order": ["project-a", "project-b"],
            "project-id-migrations": {"project-a": "app-project-a"},
            "thread-project-assignments": {
                "thread-a": "project-a",
                "thread-b": "app-project-a",
                "thread-c": None,
            },
        }

    @staticmethod
    def _payload(state: dict) -> bytes:
        return json.dumps(state, sort_keys=True).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
