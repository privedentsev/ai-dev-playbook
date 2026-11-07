"""Cheap regressions for the version/fixture failures from PB-051/052.

Run these before the full suite. Keep expensive self-copy/close pilots in the
full suite; importing the modules here does not collect their other classes.
"""
import unittest


def load_tests(loader, tests, pattern):
    return loader.loadTestsFromNames([
        'test_cli.AdoptTest.test_adopt_conflicting_version',
        'test_pb028.Pb028CompatibilityAndUpdateTest.test_real_002_artifact_update_with_custom_paths_and_evidence',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A1_symlink_in_scope_and_inputs_rejected',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A2_null_plan_and_null_inputs_rejected',
        'test_task_workflow_schema.AuditFindingsCoverageTest.test_PB_031_A4_v2_card_rejected_without_task_workflow_opt_in',
        'test_document_links.RewriteTest.test_unrelated_move_preserves_authored_destinations',
    ])


if __name__ == '__main__':
    unittest.main()
