import copy
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import review
from test_review import fixture


class SnapshotDownloadTests(unittest.TestCase):
    def test_pasted_concatenated_links_are_rejected(self):
        with self.assertRaises(ValueError):
            review.parse_sheet_id(review.SHEET_URL + review.SHEET_URL)
        self.assertEqual(review.parse_sheet_id(review.SHEET_URL + "?usp=sharing#gid=2"), review.SHEET_ID)

    def test_independent_tabs_overlap_without_changing_data_or_order(self):
        original = fixture()["sheets"]
        barrier = threading.Barrier(len(review.TABS), timeout=3)
        seen = []
        lock = threading.Lock()
        def download(title, sheet_id):
            with lock:
                seen.append((title, sheet_id, threading.get_ident()))
            barrier.wait()
            return copy.deepcopy(original[title])
        with patch.object(review, "download_tab", side_effect=download):
            actual = review.download_snapshot(sheet_id=review.SHEET_URL)
        self.assertEqual(actual["sheets"], original)
        self.assertEqual(list(actual["sheets"]), list(review.TABS))
        self.assertEqual(len({entry[2] for entry in seen}), 4)
        self.assertEqual({entry[1] for entry in seen}, {review.SHEET_ID})

    def test_any_failed_tab_rejects_the_snapshot(self):
        barrier = threading.Barrier(4, timeout=3)
        def download(title, sheet_id):
            barrier.wait()
            if title == "5_문항":
                raise ValueError("test download failure")
            return fixture()["sheets"][title]
        with patch.object(review, "download_tab", side_effect=download):
            with self.assertRaisesRegex(ValueError, "test download failure"):
                review.download_snapshot()

    def test_pre_cancelled_request_performs_no_download(self):
        event = threading.Event()
        event.set()
        with patch.object(review, "download_tab") as download:
            with self.assertRaises(review.Cancelled):
                review.download_snapshot(cancel=event)
            download.assert_not_called()

    def test_cancelled_during_reads_does_not_return_partial_data(self):
        event = threading.Event()
        barrier = threading.Barrier(4, timeout=3)
        def download(title, sheet_id):
            barrier.wait()
            event.set()
            return fixture()["sheets"][title]
        with patch.object(review, "download_tab", side_effect=download):
            with self.assertRaises(review.Cancelled):
                review.download_snapshot(cancel=event)


if __name__ == "__main__":
    unittest.main()
