import unittest

from benchmarks.run_setr_epoch_sweep import choose_by_source_dev


class EpochSweepTests(unittest.TestCase):
    def test_selection_uses_source_dev_only_and_breaks_ties_by_epoch(self):
        manifests = {e: {"epochs": e, "fresh_reload_verified": True,
                         "final_teacher_dev_loss": loss, "external_ic": 1 - loss}
                     for e, loss in [(1, .52), (3, .4), (5, .4)]}
        self.assertEqual(choose_by_source_dev(manifests), 3)
        manifests[5]["external_ic"] = 100
        self.assertEqual(choose_by_source_dev(manifests), 3)

    def test_rejects_unverified_mismatched_or_nonfinite_checkpoints(self):
        for fields in [{"fresh_reload_verified": False}, {"epochs": 2},
                       {"final_teacher_dev_loss": float("nan")}]:
            m = {"epochs": 3, "fresh_reload_verified": True, "final_teacher_dev_loss": .4, **fields}
            with self.assertRaises(ValueError):
                choose_by_source_dev({3: m})
