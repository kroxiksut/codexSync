"""The state shape the Electron Codex desktop build actually writes.

Guardian rejected this shape as `UNKNOWN_SCHEMA` and quarantined every real
state file, so no snapshot was ever committed and `latest-good` never existed —
the protection 0.2 exists to provide was inert on a real machine while the whole
suite stayed green against fixtures written to the assumed shape.

These fixtures are modelled on an observed file: bindings carry
`projectKind`/`projectId`, and app-server ids live in a per-host map rather than
a flat `project-id-migrations` table.
"""
from __future__ import annotations

import json
import unittest

from codexsync.guardian_models import ValidationStatus
from codexsync.guardian_schema import (
    supports_project_creation,
    BROKEN_BINDING_REFERENCE,
    BROKEN_ORDER_REFERENCE,
    ELECTRON_V2_SCHEMA,
    LEGACY_V1_SCHEMA,
    UNKNOWN_SCHEMA,
    validate_global_state_references,
)


HOST = "local:C:\\Users\\user\\.codex"


def _electron_state(**overrides) -> dict:
    state = {
        "local-projects": {
            "p1": {"id": "p1", "name": "one", "rootPaths": ["C:/a"], "createdAt": 1, "updatedAt": 2},
            "p2": {"id": "p2", "name": "two", "rootPaths": ["C:/b"], "createdAt": 1, "updatedAt": 2},
        },
        "project-order": ["p1", "p2"],
        "thread-project-assignments": {
            "t1": {"projectKind": "local", "projectId": "p1"},
        },
        "app-server-project-id-by-legacy-project-id-by-host": {HOST: {"p1": "as1", "p2": "as2"}},
        "app-server-projects-migration-by-host": {
            HOST: {"version": 1, "projectsMigrated": True, "threadAssignmentsMigrated": True}
        },
        "selected-project": {"projectId": "p1", "type": "local"},
        "thread-writable-roots": {"t1": ["C:/a"]},
        "electron-main-window-bounds": {"x": 0, "y": 0},
    }
    state.update(overrides)
    return state


def _payload(state: dict) -> bytes:
    return json.dumps(state, ensure_ascii=False).encode("utf-8")


class ElectronStateSchemaTests(unittest.TestCase):
    def test_the_observed_desktop_shape_validates(self) -> None:
        report = validate_global_state_references(_payload(_electron_state()))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, ELECTRON_V2_SCHEMA)
        self.assertEqual(report.project_count, 2)
        self.assertEqual(report.binding_count, 1)

    def test_a_binding_to_an_app_server_id_resolves_through_the_host_map(self) -> None:
        state = _electron_state(
            **{"thread-project-assignments": {"t1": {"projectKind": "app-server", "projectId": "as2"}}}
        )
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.PASS)

    def test_a_binding_to_an_unknown_project_is_rejected(self) -> None:
        state = _electron_state(
            **{"thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "gone"}}}
        )
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertIn(BROKEN_BINDING_REFERENCE, report.codes)

    def test_a_broken_order_is_still_caught_under_this_schema(self) -> None:
        state = _electron_state(**{"project-order": ["p1", "p2", "ghost"]})
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertIn(BROKEN_ORDER_REFERENCE, report.codes)

    def test_an_unfamiliar_binding_kind_is_unknown_rather_than_half_understood(self) -> None:
        state = _electron_state(
            **{"thread-project-assignments": {"t1": {"projectKind": "quantum", "projectId": "p1"}}}
        )
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertIn(UNKNOWN_SCHEMA, report.codes)

    def test_an_unfamiliar_binding_field_set_is_unknown(self) -> None:
        state = _electron_state(
            **{"thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "p1", "extra": 1}}}
        )
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertIn(UNKNOWN_SCHEMA, report.codes)

    def test_a_malformed_host_map_is_unknown(self) -> None:
        state = _electron_state(
            **{"app-server-project-id-by-legacy-project-id-by-host": {HOST: {"p1": 7}}}
        )
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
        self.assertIn(UNKNOWN_SCHEMA, report.codes)

    def test_the_legacy_shape_is_still_claimed_by_the_legacy_adapter(self) -> None:
        """The new adapter must not quietly swallow states v1 already handled."""
        legacy = {
            "local-projects": {"p1": {}},
            "project-order": ["p1"],
            "thread-project-assignments": {"t1": {"namespace": "legacy", "project_id": "p1"}},
        }
        report = validate_global_state_references(_payload(legacy))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, LEGACY_V1_SCHEMA)

    def test_a_state_with_neither_marker_is_not_claimed_as_electron(self) -> None:
        """No bindings and no host map: nothing identifies the runtime family."""
        bare = {"local-projects": {"p1": {}}, "project-order": ["p1"]}
        report = validate_global_state_references(_payload(bare))
        # The legacy adapter accepts it; what matters is that it is not
        # mislabelled as the newer schema.
        self.assertNotEqual(report.schema_id, ELECTRON_V2_SCHEMA)

    def test_an_electron_state_with_no_bindings_yet_is_not_claimed_as_legacy(self) -> None:
        """The dangerous ambiguity: a fresh desktop state has nothing assigned.

        Bindings and migrations are all the legacy adapter reads, and both are
        empty here, so it used to claim the state and the repair writer would
        then create legacy-shaped project entries inside an Electron one. The
        project entry itself settles it: `rootPaths` belongs to Electron.
        """
        fresh = {
            "local-projects": {
                "p1": {"id": "p1", "name": "alpha", "rootPaths": ["D:/alpha"],
                       "createdAt": 1, "updatedAt": 2}
            },
            "project-order": ["p1"],
            "thread-project-assignments": {},
            "app-server-project-id-by-legacy-project-id-by-host": {},
        }
        report = validate_global_state_references(_payload(fresh))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, ELECTRON_V2_SCHEMA)
        self.assertFalse(
            supports_project_creation(report.schema_id),
            "creating an entry here would have to invent the fields around the root",
        )


if __name__ == "__main__":
    unittest.main()
