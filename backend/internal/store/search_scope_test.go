package store

import (
	"context"
	"errors"
	"slices"
	"strconv"
	"testing"
)

func TestScopeForSearch(t *testing.T) {
	ctx := context.Background()
	s, err := Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { s.DB.Close() })
	create := func(collection string) string {
		t.Helper()
		base, err := s.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": collection, "name": collection})
		if err != nil {
			t.Fatal(err)
		}
		return String(base["id"])
	}
	parent, child, outside := create("Parent"), create("Child"), create("Outside")
	content := []byte("%PDF-fixture")
	sha := Hash(content)
	_, err = s.PutExtracted(ctx, sha, content, Object{
		"pages":  []any{Object{"page": 1, "text": "synthetic paper"}},
		"report": Object{"total_pages": 1, "native_pages": 1, "low_text_pages": 0, "empty_pages": 0},
	})
	if err != nil {
		t.Fatal(err)
	}
	doc := Object{"key": "Paper", "kind": "document", "hash": sha}
	ext := Object{"key": "External", "kind": "note", "body": "synthetic external note"}
	for _, b := range []struct {
		id       string
		name     string
		children bool
		members  []any
	}{
		{parent, "Parent", true, []any{doc, ext}},
		{child, "Child", false, []any{doc, ext, Object{"key": "Duplicate", "kind": "document", "hash": sha}}},
		{outside, "Outside", false, []any{Object{"key": "Outside", "kind": "note", "body": "outside"}}},
	} {
		if _, err := s.Sync(ctx, b.id, Object{"name": b.name, "include_children": b.children, "entities": b.members, "complete": true, "expected_revision": 0}); err != nil {
			t.Fatal(err)
		}
	}
	note, err := s.SaveNote(ctx, child, "", Object{"body": "initial note"})
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range []Object{{"body": "draft", "confirmed": false}, {"body": "archived", "archived": true}} {
		if _, err := s.SaveNote(ctx, child, "", d); err != nil {
			t.Fatal(err)
		}
	}
	pdf := sha + ".pdf"
	external := "notes/" + Identity("fixture", "user:0", "External") + "-1.md"
	noteSource := func(rev int) string { return "notes/" + String(note["id"]) + "-" + strconv.Itoa(rev) + ".md" }
	assertScope := func(ids []string, scope string, filter any, want ...string) SearchScope {
		t.Helper()
		got, err := s.ScopeForSearch(ctx, ids, scope, filter)
		slices.Sort(want)
		if err != nil || !slices.Equal(got.Sources, want) {
			t.Fatalf("%s sources = %v, want %v (%v)", scope, got.Sources, want, err)
		}
		return got
	}
	ids := []string{parent, child, parent}
	assertScope([]string{parent}, "both", nil, pdf, external)
	before := assertScope(ids, "both", nil, pdf, external, noteSource(1))
	assertScope(ids, "original", []string{pdf}, pdf)
	assertScope(ids, "original", []string{"missing.pdf"})
	assertScope(ids, "both", []any{"missing.pdf"}, external, noteSource(1))
	if _, err := s.SaveNote(ctx, child, String(note["id"]), Object{"body": "updated note", "expected_revision": 1}); err != nil {
		t.Fatal(err)
	}
	after := assertScope(ids, "notes", nil, external, noteSource(2))
	if len(after.Revisions) != 2 || after.Revisions[child] != before.Revisions[child]+1 || after.Revisions[parent] != before.Revisions[parent] {
		t.Fatalf("unexpected revision change: %v to %v", before.Revisions, after.Revisions)
	}
	if _, err := s.Archive(ctx, child, Object{"archived": true, "expected_revision": after.Revisions[child]}); err != nil {
		t.Fatal(err)
	}
	if _, err := s.ScopeForSearch(ctx, ids, "both", nil); !errors.Is(err, ErrConflict) {
		t.Fatalf("archived knowledge base returned %v", err)
	}
}
