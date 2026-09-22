"""Isolated synthetic knowledge bases; no models, Zotero library, or cloud account."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from starlette.applications import Starlette
from starlette.testclient import TestClient
from research_assistant.knowledge.store import KnowledgeStore, digest
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.backup import export_backup, restore_backup
from research_assistant.memory.store import Conflict
from research_assistant.web.knowledge_api import KnowledgeAPI
from tests.test_reading_memory import pdf


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        self.store = KnowledgeStore(self.root)
        self.service = KnowledgeService(self.store)
        self.file = Path(self.temp.name) / "synthetic.pdf"
        pdf(self.file)
        self.content = self.file.read_bytes()
        self.sha = digest(self.content)
        self.store.put_object(self.sha, self.content)
        self.a, self.b = self.base("A"), self.base("B")
        self.doc = {"key": "PDF00001", "kind": "document", "title": "Synthetic scientific evidence",
                    "parent_key": "PAPER001", "hash": self.sha}

    def base(self, collection):
        return self.store.create({"connection": "fixture", "library": "user:0",
                                  "collection": collection, "name": collection})

    def sync(self, base, entities, **extra):
        return self.store.sync(base["id"], {
            "expected_revision": self.store.base(base["id"])["revision"], "name": base["name"],
            "include_children": True, "complete": True, "entities": entities, **extra,
        })

    def search(self, bases, query="scientific evidence", **extra):
        return self.service.search({"kb_ids": [b["id"] for b in bases], "query": query, **extra})["results"]

    def note(self, key="NOTE0001", body="Unique private telescope observation", **extra):
        return {"key": key, "kind": "note", "title": "Research note", "body": body, **extra}

    def test_shared_pdf_parsed_once_and_removed_from_one_base_only(self):
        self.sync(self.a, [self.doc])
        self.sync(self.b, [self.doc])
        with patch("research_assistant.ingestion.pdf_extractor.PDFExtractor.extract", side_effect=AssertionError("duplicate parse")):
            self.assertTrue(self.store.put_object(self.sha, self.content)["cached"])
        self.sync(self.a, [])
        self.assertEqual(self.search([self.a]), [])
        self.assertTrue(self.search([self.b]))
        self.assertTrue(self.store.object_exists(self.sha))

    def test_empty_or_unknown_scope_never_becomes_global(self):
        self.sync(self.a, [self.doc])
        self.assertEqual(self.search([self.b]), [])
        with self.assertRaises(ValueError):
            self.service.search({"kb_ids": [], "query": "evidence"})
        with self.assertRaises(KeyError):
            self.service.search({"kb_ids": ["unknown"], "query": "evidence"})

    def test_partial_snapshot_or_missing_attachment_does_not_remove_members(self):
        self.sync(self.a, [self.doc])
        for change in ({"complete": False}, {"entities": [{**self.doc, "hash": "0" * 64}]}):
            with self.assertRaises(ValueError):
                self.sync(self.a, change.get("entities", []), **{k: v for k, v in change.items() if k != "entities"})
            self.assertEqual(len(self.store.snapshot([self.a["id"]])["entities"]), 1)

    def test_revision_conflict_and_idempotent_replay(self):
        result = self.sync(self.a, [self.doc], expected_revision=0)
        again = self.sync(self.a, [self.doc], expected_revision=0)
        self.assertEqual(result["revision"], again["revision"])
        with self.assertRaises(Conflict):
            self.sync(self.a, [], expected_revision=0)

    def test_rename_preserves_identity_and_index_content(self):
        self.sync(self.a, [self.doc], name="Renamed")
        again = self.base("A")
        self.assertEqual(again["id"], self.a["id"])
        self.assertEqual(again["name"], "Renamed")
        self.assertTrue(self.search([again]))

    def test_same_filename_different_keys_does_not_collide(self):
        self.sync(self.a, [self.doc, {**self.doc, "key": "PDF00002"}])
        self.assertEqual(len(self.store.snapshot([self.a["id"]])["entities"]), 2)
        self.assertEqual(len(self.search([self.a])), 1)

    def test_shared_note_update_invalidates_other_base_snapshot(self):
        self.sync(self.a, [self.note()])
        self.sync(self.b, [self.note()])
        before = self.store.snapshot([self.b["id"]])
        self.sync(self.a, [self.note(body="Changed note")])
        with self.assertRaises(Conflict):
            self.store.assert_current(before)
        self.assertEqual(self.store.snapshot([self.b["id"]])["entities"][0]["data"]["body"], "Changed note")

    def test_note_scope_and_drafts_and_topic_membership(self):
        self.sync(self.a, [self.doc, self.note(), self.note("DRAFT001", "private draft nebula", confirmed=False)])
        self.sync(self.b, [self.doc])
        self.store.save_note(self.a["id"], {"body": "private telescope hypothesis", "title": "Topic"})
        hits = self.search([self.a], "telescope", scope="notes")
        self.assertEqual({h["kind"] for h in hits}, {"external_note", "user_note"})
        self.assertEqual(self.search([self.b], "telescope", scope="notes"), [])
        self.assertEqual(self.search([self.a], "nebula"), [])
        self.assertTrue(all(h["kind"] == "original" for h in self.search([self.a], scope="original")))

    def test_note_compare_and_swap_and_history(self):
        first = self.store.save_note(self.a["id"], {"body": "first note"})
        self.store.save_note(self.a["id"], {"body": "second note", "expected_revision": 1}, first["id"])
        with self.assertRaises(Conflict):
            self.store.save_note(self.a["id"], {"body": "lost edit", "expected_revision": 1}, first["id"])
        with self.store.transaction() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM note_revisions").fetchone()[0], 2)

    def test_archive_preserves_data_but_excludes_search(self):
        self.sync(self.a, [self.doc])
        base = self.store.base(self.a["id"])
        self.store.set_archive(base["id"], {"archived": True, "expected_revision": base["revision"]})
        with self.assertRaises(Conflict):
            self.search([self.a])
        self.assertTrue(self.store.object_exists(self.sha))
        base = self.store.base(base["id"])
        self.store.set_archive(base["id"], {"archived": False, "expected_revision": base["revision"]})
        self.assertTrue(self.search([self.a]))

    def test_attachment_tamper_and_hash_validation(self):
        with self.assertRaises(ValueError):
            self.store.put_object("0" * 64, self.content)
        (self.store.objects / (self.sha + ".pdf")).write_bytes(b"tampered")
        self.assertFalse(self.store.object_exists(self.sha))

    def test_annotation_never_masquerades_as_original(self):
        annotation = {"key": "ANN00001", "kind": "annotation", "title": "Annotation", "body": "Scientific evidence",
                      "comment": "external commentary", "attachment_key": self.doc["key"], "page": 1}
        self.sync(self.a, [self.doc, annotation])
        hits = self.search([self.a], scope="notes")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["kind"], "external_annotation")
        self.assertIn("page=1", hits[0]["url"])

    def test_backup_restore_roundtrip_and_no_credentials(self):
        self.sync(self.a, [self.doc, self.note()])
        self.store.save_note(self.a["id"], {"body": "Personal evidence note"})
        (self.store.home / "connection-token").write_text("SECRET-TOKEN")
        path = Path(self.temp.name) / "backup.zip"
        export_backup(self.store, path)
        with zipfile.ZipFile(path) as z:
            self.assertNotIn("connection-token", z.namelist())
            self.assertTrue(any(n.endswith(".md") for n in z.namelist()))
        restored = Path(self.temp.name) / "restored"
        restore_backup(path, restored)
        store = KnowledgeStore(restored)
        self.assertEqual(store.snapshot([self.a["id"]]), self.store.snapshot([self.a["id"]]))
        self.assertTrue(KnowledgeService(store).search({"kb_ids": [self.a["id"]], "query": "scientific"})["results"])
        self.assertFalse((store.home / "connection-token").exists())
        with self.assertRaises(ValueError):
            restore_backup(path, restored)

    def test_backup_tampering_and_zip_traversal_rejected(self):
        path = Path(self.temp.name) / "bad.zip"
        for name in ("../outside", "knowledge.sqlite3"):
            manifest = {"format": "research-zotero-backup", "version": 1, "files": {name: "0" * 64}}
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(name, "bad")
                z.writestr("manifest.json", json.dumps(manifest))
            with self.assertRaises(ValueError):
                restore_backup(path, Path(self.temp.name) / "new")
            self.assertFalse((Path(self.temp.name) / "new").exists())

    def test_http_auth_origin_and_no_legacy_side_effects(self):
        api = KnowledgeAPI(self.root, token="synthetic-token")
        client = TestClient(Starlette(routes=api.routes()))
        self.addCleanup(client.close)
        self.assertEqual(client.get("/api/knowledge/bases").status_code, 401)
        headers = {"Authorization": "Bearer synthetic-token"}
        self.assertEqual(client.get("/api/knowledge/status", headers=headers).json()["protocol"], 1)
        self.assertEqual(client.post("/api/knowledge/bases", headers={**headers, "Origin": "https://evil.invalid"}, json={}).status_code, 403)
        self.assertEqual(client.post("/api/knowledge/search", headers=headers, json={"kb_ids": [], "query": "text"}).status_code, 400)
        self.assertFalse((self.root / ".data/reading.sqlite3").exists())
        self.assertFalse((self.root / "library").exists())

    def test_pdf_binary_http_and_consent(self):
        api = KnowledgeAPI(self.root, token="synthetic-token")
        with TestClient(Starlette(routes=api.routes())) as client:
            headers = {"Authorization": "Bearer synthetic-token"}
            response = client.put("/api/knowledge/objects/" + self.sha, content=self.content, headers=headers)
            self.assertTrue(response.json()["cached"])
            response = client.post("/api/knowledge/sync/" + self.a["id"], headers=headers, json={"complete": True, "entities": []})
            self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
