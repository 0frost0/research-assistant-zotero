package qa

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

type comparisonWorker struct {
	mu        sync.Mutex
	calls     int
	duplicate bool
}

func (w *comparisonWorker) Search(_ context.Context, _ string, d Object) (Object, error) {
	source := list(d["source_ids"])[0]
	return Object{"results": []Object{{"source_id": source, "quote": "Study evidence text.", "page": 1, "modality": "text"}}}, nil
}
func (w *comparisonWorker) Call(_ context.Context, _ string, _ string, d Object) (Object, error) {
	w.mu.Lock()
	w.calls++
	w.mu.Unlock()
	var value Object
	switch d["role"] {
	case "analyst":
		doc := d["payload"].(map[string]any)["document"].(map[string]any)
		ref := array(doc["evidence"])[0]["ref"]
		value = Object{"findings": []Object{{"aspect": "method", "statement": "Study finding", "evidence_ids": []any{ref}}}, "gaps": []string{}}
	case "synthesizer":
		value = Object{"comparisons": []Object{{"aspect": "method", "statement": "Methods differ", "relation": "difference", "evidence_ids": []string{"D1E1", "D2E1"}}, {"aspect": "method", "statement": "Bad single-paper comparison", "relation": "agreement", "evidence_ids": []string{"D1E1"}}}, "profiles": []Object{}}
	case "reviewer":
		vs := []Object{{"claim_index": 0, "verdict": "supported", "reason": "supported"}}
		if w.duplicate {
			vs = append(vs, vs[0])
		}
		value = Object{"verdicts": vs}
	}
	return Object{"value": asJSON(value), "invalid": false}, nil
}
func TestComparisonBudgetAndCrossSourceReview(t *testing.T) {
	s, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.DB.Close()
	ctx := context.Background()
	b, _ := s.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
	entities := []any{}
	sources := []string{}
	for i := 0; i < 2; i++ {
		content := []byte(fmt.Sprintf("%%PDF-test-%d", i))
		sha := store.Hash(content)
		if err = os.WriteFile(filepath.Join(s.Home, "objects", sha+".pdf"), content, 0600); err != nil {
			t.Fatal(err)
		}
		_, err = s.DB.Exec("INSERT INTO objects VALUES(?,?,?)", sha, `[{"page":1,"text":"Study evidence text."}]`, `{"total_pages":1,"native_pages":1,"low_text_pages":[],"empty_pages":[]}`)
		if err != nil {
			t.Fatal(err)
		}
		entities = append(entities, Object{"key": fmt.Sprintf("PDF%d", i), "kind": "document", "hash": sha})
		sources = append(sources, sha+".pdf")
	}
	_, err = s.Sync(ctx, store.String(b["id"]), Object{"name": "A", "complete": true, "include_children": true, "expected_revision": 0, "entities": entities})
	if err != nil {
		t.Fatal(err)
	}
	for _, duplicate := range []bool{false, true} {
		w := &comparisonWorker{duplicate: duplicate}
		engine := &Engine{Store: s, Worker: w}
		result, err := engine.Compare(ctx, "fixture", Object{"question": "Compare methods", "allow_external": true, "kb_ids": []string{store.String(b["id"])}, "sources": sources})
		if err != nil {
			t.Fatal(err)
		}
		if w.calls != 4 || result["call_budget"] != 4 {
			t.Fatal("unexpected model budget", result)
		}
		if duplicate && len(array(result["comparisons"])) != 0 {
			t.Fatal("duplicate verdict accepted")
		}
		if !duplicate && len(array(result["comparisons"])) != 1 {
			t.Fatal("cross-source filtering failed", result)
		}
	}
}
