package store

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"testing"
)

func python(t *testing.T, root, script string) Object {
	t.Helper()
	exe := os.Getenv("PYTHON_EXECUTABLE")
	if exe == "" {
		exe = "python"
	}
	cmd := exec.Command(exe, "-X", "utf8", "-c", script, root)
	cmd.Dir = filepath.Join("..", "..", "..")
	raw, e := cmd.CombinedOutput()
	if e != nil {
		t.Fatalf("Python compatibility oracle: %v: %s", e, raw)
	}
	v, e := Decode(string(raw))
	if e != nil {
		t.Fatal(e, string(raw))
	}
	return v
}
func TestPythonRoundTrip(t *testing.T) {
	root := t.TempDir()
	ctx := context.Background()
	oracle := python(t, root, `
import sys,json
from research_assistant.knowledge.store import KnowledgeStore,identity,encoded
s=KnowledgeStore(sys.argv[1])
b=s.create(dict(connection='fixture',library='user:0',collection='ABC',name='中文 <>&\u2028'))
b=s.sync(b['id'],dict(name=b['name'],include_children=True,complete=True,expected_revision=0,entities=[dict(key='NOTE1',kind='note',body='original')]))
n=s.save_note(b['id'],dict(body='my note',confirmed=True))
print(json.dumps(dict(base=b['id'],note=n['id'],identity=identity('中文','literal\\u2028','\u2028'),canonical=encoded(['literal\\u2028','\u2028'])),ensure_ascii=False))
`)
	s, e := Open(root)
	if e != nil {
		t.Fatal(e)
	}
	defer s.DB.Close()
	if Identity("中文", `literal\u2028`, "\u2028") != oracle["identity"] {
		t.Fatal("Python UUID5 mismatch")
	}
	if Canonical([]string{`literal\u2028`, "\u2028"}) != oracle["canonical"] {
		t.Fatal("Python JSON escaping mismatch")
	}
	id := String(oracle["base"])
	snap, e := s.Snapshot(ctx, []string{id})
	if e != nil {
		t.Fatal(e)
	}
	if len(snap["entities"].([]Object)) != 1 {
		t.Fatal(snap)
	}
	n, e := s.SaveNote(ctx, id, String(oracle["note"]), Object{"body": "updated 中文", "expected_revision": 1})
	if e != nil {
		t.Fatal(e)
	}
	if n["revision"] != int64(2) {
		t.Fatal(n)
	}
	if _, e = s.SaveNote(ctx, id, String(oracle["note"]), Object{"body": "stale", "expected_revision": 1}); !errors.Is(e, ErrConflict) {
		t.Fatalf("stale write: %v", e)
	}
	d := Object{"name": "中文 <>&\u2028", "include_children": true, "complete": true, "expected_revision": 3, "entities": []any{Object{"key": "NOTE1", "kind": "note", "body": "updated external"}}}
	updated, e := s.Sync(ctx, id, d)
	if e != nil {
		t.Fatal(e)
	}
	if updated["revision"] != int64(4) {
		t.Fatal(updated)
	}
	replayed, e := s.Sync(ctx, id, d)
	if e != nil || replayed["unchanged"] != true {
		t.Fatal(replayed, e)
	}
	check := python(t, root, `
import sys,json
from research_assistant.knowledge.store import KnowledgeStore
s=KnowledgeStore(sys.argv[1]); b=s.bases()[0]; snap=s.snapshot([b['id']]); n=snap['notes'][0]
replay=s.sync(b['id'],dict(name=b['name'],include_children=True,complete=True,expected_revision=b['revision'],entities=[dict(key='NOTE1',kind='note',body='updated external')]))
print(json.dumps(dict(revision=b['revision'],note_revision=n['revision'],body=n['data']['body'],unchanged=replay['unchanged'],history=len(s.note_history(n['id'])))))
`)
	if check["body"] != "updated 中文" || check["unchanged"] != true || check["history"] != json.Number("2") {
		t.Fatal(check)
	}
}
func TestConcurrentRevisionAndRollback(t *testing.T) {
	s, e := Open(t.TempDir())
	if e != nil {
		t.Fatal(e)
	}
	defer s.DB.Close()
	ctx := context.Background()
	b, e := s.Create(ctx, Object{"connection": "test", "library": "user:0", "collection": "A", "name": "A"})
	if e != nil {
		t.Fatal(e)
	}
	id := String(b["id"])
	n, e := s.SaveNote(ctx, id, "", Object{"body": "one"})
	if e != nil {
		t.Fatal(e)
	}
	var wg sync.WaitGroup
	result := make(chan error, 2)
	for _, body := range []string{"two", "three"} {
		wg.Add(1)
		go func(body string) {
			defer wg.Done()
			_, e := s.SaveNote(ctx, id, String(n["id"]), Object{"body": body, "expected_revision": 1})
			result <- e
		}(body)
	}
	wg.Wait()
	close(result)
	wins, conflicts := 0, 0
	for e := range result {
		if e == nil {
			wins++
		} else if errors.Is(e, ErrConflict) {
			conflicts++
		} else {
			t.Fatal(e)
		}
	}
	if wins != 1 || conflicts != 1 {
		t.Fatal(wins, conflicts)
	}
	_, e = s.Sync(ctx, id, Object{"name": "bad", "complete": true, "include_children": true, "expected_revision": 2, "entities": []any{Object{"kind": "note", "key": "N", "body": "valid"}, Object{"kind": "document", "key": "D", "hash": "invalid"}}})
	if e == nil {
		t.Fatal("invalid object accepted")
	}
	snap, e := s.Snapshot(ctx, []string{id})
	if e != nil {
		t.Fatal(e)
	}
	if len(snap["entities"].([]Object)) != 0 || snap["bases"].([]Object)[0]["revision"] != int64(2) {
		t.Fatal("partial sync changed data")
	}
}
func TestCancelWinsAndRestartInterrupts(t *testing.T) {
	path := filepath.Join(t.TempDir(), "qa_jobs.sqlite3")
	j, e := OpenJobs(path)
	if e != nil {
		t.Fatal(e)
	}
	ctx := context.Background()
	a, e := j.Create(ctx, Object{"query": "cancel", "kb_ids": []string{"A"}})
	if e != nil {
		t.Fatal(e)
	}
	id := String(a["job_id"])
	if e = j.MarkRunning(ctx, id); e != nil {
		t.Fatal(e)
	}
	if _, e = j.Cancel(ctx, id); e != nil {
		t.Fatal(e)
	}
	late, e := j.Finish(ctx, id, "succeeded", Object{"status": "answered"}, nil)
	if e != nil || late["state"] != "cancelled" {
		t.Fatal(late, e)
	}
	b, e := j.Create(ctx, Object{"query": "restart"})
	if e != nil {
		t.Fatal(e)
	}
	if e = j.DB.Close(); e != nil {
		t.Fatal(e)
	}
	j, e = OpenJobs(path)
	if e != nil {
		t.Fatal(e)
	}
	defer j.DB.Close()
	current, e := j.Public(ctx, String(b["job_id"]))
	if e != nil || current["state"] != "interrupted" {
		t.Fatal(current, e)
	}
	for i := 0; i < 3; i++ {
		next, e := j.Retry(ctx, String(current["job_id"]))
		if e != nil {
			t.Fatal(e)
		}
		current, e = j.Finish(ctx, String(next["job_id"]), "failed", nil, "test")
		if e != nil {
			t.Fatal(e)
		}
	}
	if _, e = j.Retry(ctx, String(current["job_id"])); !errors.Is(e, ErrInvalid) {
		t.Fatalf("unbounded retry: %v", e)
	}
}

func TestGoBackupRestoresWithPython(t *testing.T) {
	root := t.TempDir()
	s, err := Open(root)
	if err != nil {
		t.Fatal(err)
	}
	defer s.DB.Close()
	ctx := context.Background()
	b, err := s.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = s.SaveNote(ctx, String(b["id"]), "", Object{"body": "backup note"}); err != nil {
		t.Fatal(err)
	}
	if err = s.InitParsing(ctx); err != nil {
		t.Fatal(err)
	}
	archive := filepath.Join(root, "backup.zip")
	if err = s.Export(ctx, archive); err != nil {
		t.Fatal(err)
	}
	result := python(t, root, `
import sys,json
from pathlib import Path
from research_assistant.knowledge.backup import restore_backup
from research_assistant.knowledge.store import KnowledgeStore
root=Path(sys.argv[1]); target=restore_backup(root/'backup.zip',root/'restored'); s=KnowledgeStore(target); b=s.bases()[0]
print(json.dumps(dict(body=s.snapshot([b['id']])['notes'][0]['data']['body'])))
`)
	if result["body"] != "backup note" {
		t.Fatal(result)
	}
}
