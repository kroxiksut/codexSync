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
    PROJECT_NOT_IN_ORDER,
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

    def test_a_project_the_order_omits_is_a_warning_not_a_rejection(self) -> None:
        """Observed on a real desktop state: Codex itself wrote projects its order omits.

        Holding this shape to the v1 completeness rule quarantined every real
        snapshot and made every global-state commit refuse its own result.
        """
        state = _electron_state(**{"project-order": ["p1"]})
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.PASS_WITH_WARNING)
        self.assertEqual(report.codes, (PROJECT_NOT_IN_ORDER,))
        self.assertEqual(report.project_count, 2)

    def test_an_omitted_project_does_not_hide_a_broken_binding(self) -> None:
        state = _electron_state(**{
            "project-order": ["p1"],
            "thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "gone"}},
        })
        report = validate_global_state_references(_payload(state))
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertIn(BROKEN_BINDING_REFERENCE, report.codes)

    def test_the_legacy_shape_still_requires_a_complete_order(self) -> None:
        legacy = {"local-projects": {"a": {"root": "C:/a"}, "b": {"root": "C:/b"}}, "project-order": ["a"]}
        report = validate_global_state_references(_payload(legacy))
        self.assertEqual(report.status, ValidationStatus.INVALID)
        self.assertEqual(report.schema_id, LEGACY_V1_SCHEMA)

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


#: Keys of the state a fresh Linux Codex 26.1002 wrote before it held a
#: project (2026-10-09); the values that are not project keys are trimmed.
_NEVER_HAD_A_PROJECT = {
    "app-server-projects-migration-by-host": {
        "local:/home/someone/.codex": {
            "version": 1, "projectsMigrated": True, "threadAssignmentsMigrated": False,
            "pendingThreadAssignmentIds": ["t0"],
        },
    },
    "electron-main-window-bounds": {"x": 0, "y": 0},
    "local-projects": {},
    "projectless-thread-ids": ["t0"],
    "selected-project": None,
    "sidebar-project-thread-orders": {},
    "thread-workspace-root-hints": {"t0": "/home/someone/Documents/Codex"},
}


class NeverHadAProjectTests(unittest.TestCase):
    def test_the_observed_fresh_state_is_the_desktop_schema_with_nothing_in_it(self) -> None:
        report = validate_global_state_references(_payload(_NEVER_HAD_A_PROJECT))
        self.assertEqual(report.status, ValidationStatus.PASS)
        self.assertEqual(report.schema_id, ELECTRON_V2_SCHEMA)
        self.assertEqual((report.project_count, report.binding_count), (0, 0))

    def test_any_project_trace_is_not_this_shape(self) -> None:
        traces = {
            "a project without an order": {"local-projects": {"p1": {"rootPaths": ["C:/a"]}}},
            "an order key": {"project-order": []},
            "a pin": {"pinned-project-ids": ["p1"]},
            "a binding": {"thread-project-assignments": {"t1": {"projectKind": "local", "projectId": "p1"}}},
            "a migration": {"project-id-migrations": {"p1": "as1"}},
            "an app-server id": {"app-server-project-id-by-legacy-project-id-by-host": {HOST: {"p1": "as1"}}},
            "no project key at all": {"local-projects": None},
        }
        for label, overrides in traces.items():
            with self.subTest(label):
                state = {**_NEVER_HAD_A_PROJECT, **overrides}
                if state["local-projects"] is None:
                    del state["local-projects"]
                report = validate_global_state_references(_payload(state))
                if "project-order" in overrides:
                    # An empty order with an empty list is the legacy adapter's, as before.
                    self.assertEqual(report.schema_id, LEGACY_V1_SCHEMA)
                    continue
                self.assertEqual(report.status, ValidationStatus.INDETERMINATE)
                self.assertIn(UNKNOWN_SCHEMA, report.codes)

    def test_a_null_binding_is_still_no_project(self) -> None:
        state = {**_NEVER_HAD_A_PROJECT, "thread-project-assignments": {"t0": None}}
        self.assertEqual(validate_global_state_references(_payload(state)).schema_id, ELECTRON_V2_SCHEMA)

    def test_a_populated_state_that_collapses_to_this_shape_is_a_shrink(self) -> None:
        """Same schema id, so the drop is measured -- not hidden behind a schema change."""
        from codexsync.guardian_shrink import PROJECT_COUNT_DROP, assess_suspicious_shrink

        try:
            from tests.test_guardian_shrink import GuardianShrinkTests
        except ImportError:  # collected with tests/ itself on sys.path
            from test_guardian_shrink import GuardianShrinkTests
        baseline = GuardianShrinkTests._baseline(19, 30, schema_id=ELECTRON_V2_SCHEMA)
        candidate = validate_global_state_references(_payload(_NEVER_HAD_A_PROJECT))
        report = assess_suspicious_shrink(candidate, baseline=baseline, baseline_verified=True)
        self.assertEqual(report.status, ValidationStatus.SUSPICIOUS)
        self.assertIn(PROJECT_COUNT_DROP, report.codes)
