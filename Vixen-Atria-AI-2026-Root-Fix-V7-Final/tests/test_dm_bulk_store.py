import tempfile
import unittest
from pathlib import Path

from bot.utils.dm_store import DMStore


class DMBulkStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tmp.name) / "dm.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_template_content_metadata_and_favorite(self):
        template_id = self.store.create_template(
            11, "Welcome", {"title": "Hi {username}"}, 9,
            content="Hello {username}", description="Onboarding", category="Community",
        )
        template = self.store.get_template(11, template_id)
        self.assertEqual(template["content"], "Hello {username}")
        self.assertEqual(template["category"], "Community")
        self.assertFalse(template["favorite"])
        self.assertTrue(self.store.set_template_favorite(11, template_id, True))
        self.assertTrue(self.store.get_template(11, template_id)["favorite"])
        self.assertEqual(len(self.store.list_templates(11, query="onboard", favorites_only=True)), 1)

    def test_history_persists_content(self):
        dm_id = self.store.record_send(11, 22, 9, None, "success", "", {"title": "Test"}, "Hello")
        history = self.store.recent_history(11)
        self.assertEqual(history[0]["dm_id"], dm_id)
        self.assertEqual(history[0]["content"], "Hello")

    def test_bulk_job_snapshot_and_item_lifecycle(self):
        job_id = self.store.create_bulk_job(
            "job-1", 11, 9, "Moderator", None, "Reason", "Hi {username}",
            {"title": "Welcome"}, {"messages_per_minute": 20, "target": {"mode": "all"}}, [22, 23, 24],
        )
        job = self.store.get_bulk_job(job_id)
        self.assertEqual(job["total"], 3)
        self.assertEqual(job["status"], "queued")
        item = self.store.claim_next_bulk_item(job_id)
        self.assertEqual(item["recipient_id"], 22)
        self.store.set_bulk_item_result(item["item_id"], "success", dm_id=101)
        self.store.increment_bulk_result(job_id, "success")
        job = self.store.get_bulk_job(job_id)
        self.assertEqual(job["success"], 1)
        self.assertEqual(job["attempted"], 1)
        self.assertEqual(self.store.bulk_items(job_id)[0]["status"], "success")

    def test_cancel_pending_is_idempotent_for_counting(self):
        job_id = self.store.create_bulk_job("job-2", 11, 9, "Moderator", None, "", "Hi", {"title": "T"}, {}, [1, 2, 3])
        self.assertEqual(self.store.cancel_pending_bulk_items(job_id), 3)
        self.assertEqual(self.store.cancel_pending_bulk_items(job_id), 0)
        self.assertEqual(self.store.get_bulk_job(job_id)["cancelled"], 3)


if __name__ == "__main__":
    unittest.main()

class DMBulkRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tmp.name) / "dm.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_restart_marks_queued_and_running_jobs_interrupted(self):
        self.store.create_bulk_job('queued', 11, 9, 'Moderator', None, '', 'Hi', {'title': 'T'}, {}, [1])
        self.store.create_bulk_job('running', 11, 9, 'Moderator', None, '', 'Hi', {'title': 'T'}, {}, [2])
        self.store.set_bulk_job_status('running', 'running')
        self.store.mark_running_jobs_interrupted()
        self.assertEqual(self.store.get_bulk_job('queued')['status'], 'interrupted')
        self.assertEqual(self.store.get_bulk_job('running')['status'], 'interrupted')

class DMBulkUncertainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DMStore(Path(self.tmp.name) / "dm.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_interrupt_marks_inflight_item_uncertain_and_blocks_safe_resume(self):
        self.store.create_bulk_job('job-u', 11, 9, 'Moderator', None, '', 'Hi', {'title': 'T'}, {}, [1, 2])
        item = self.store.claim_next_bulk_item('job-u')
        self.assertEqual(item['recipient_id'], 1)
        self.assertEqual(self.store.interrupt_bulk_job('job-u'), 1)
        job = self.store.get_bulk_job('job-u')
        self.assertEqual(job['status'], 'interrupted')
        self.assertEqual(job['uncertain'], 1)
        self.assertEqual(job['attempted'], 1)
        self.assertEqual(self.store.bulk_items('job-u')[0]['status'], 'uncertain')
