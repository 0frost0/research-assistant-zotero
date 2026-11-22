package qa

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

type fakeWorker struct {
	values   []Object
	roles    []string
	pool     []Object
	searches int
}

func (f *fakeWorker) Search(context.Context, string, Object) (Object, error) {
	f.searches++
	return Object{"backend": "test-double", "results": f.pool}, nil
}
func (f *fakeWorker) Call(_ context.Context, _ string, op string, d Object) (Object, error) {
	f.roles = append(f.roles, store.String(d["role"]))
	value := f.values[0]
	f.values = f.values[1:]
	return Object{"value": asJSON(value), "usage": Object{"input_tokens": 1, "output_tokens": 1}}, nil
}

func TestIdentifierOnlyCandidateCannotBecomeMemoryEvidence(t *testing.T) {
	hit := Object{"kind": "user_note", "quote": "EHR event intervals", "score": .8}
	if !relevant("EHR", hit) {
		t.Fatal("ordinary exact match should pass")
	}
	hit["identifier_only"] = true
	if relevant("EHR", hit) {
		t.Fatal("identifier-only candidate passed the memory evidence filter")
	}
}

func TestStrictModelOutput(t *testing.T) {
	valid := asJSON(Object{"question_relevant": true, "verdicts": []Object{{"claim_index": 0, "supported": true, "reason": "supported"}}})
	if !validReview(valid) {
		t.Fatal("valid review rejected")
	}
	for _, bad := range []any{"true", 1, nil} {
		v := asJSON(valid)
		array(v["verdicts"])[0]["supported"] = bad
		if validReview(v) {
			t.Fatalf("accepted supported=%v", bad)
		}
	}
	for _, bad := range []any{true, "0", json.Number("0.0"), -1} {
		v := asJSON(valid)
		array(v["verdicts"])[0]["claim_index"] = bad
		if validReview(v) {
			t.Fatalf("accepted claim_index=%v", bad)
		}
	}
	delete(valid, "question_relevant")
	if validReview(valid) {
		t.Fatal("missing scope accepted")
	}
}
func TestUnverifiedClaimNeverEscapes(t *testing.T) {
	s, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.DB.Close()
	ctx := context.Background()
	b, err := s.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
	if err != nil {
		t.Fatal(err)
	}
	hit := Object{"kind": "original", "source_id": "paper.pdf", "revision": 1, "page": 1, "quote": "The result is 143M parameters."}
	evidence := selectEvidence([]Object{hit}, false, false)
	for _, test := range []struct {
		name, quote         string
		supported, relevant bool
		expected            int
	}{{"valid", "143M parameters", true, true, 1}, {"invented quote", "999M parameters", true, true, 0}, {"unsupported", "143M parameters", false, true, 0}, {"wrong scope", "143M parameters", true, false, 0}} {
		t.Run(test.name, func(t *testing.T) {
			draft := Object{"claims": []Object{{"text": "143M", "citations": []Object{{"evidence_id": evidence[0]["evidence_id"], "quote": test.quote}}}}}
			f := &fakeWorker{pool: []Object{hit}, values: []Object{draft, {"question_relevant": test.relevant, "verdicts": []Object{{"claim_index": 0, "supported": test.supported, "reason": "test"}}}}}
			engine := &Engine{Store: s, Worker: f}
			result, err := engine.Ask(ctx, "fixture", Object{"query": "parameters?", "kb_ids": []string{store.String(b["id"])}, "strategy": "fixed"}, Object{"llm": Object{"model": "test", "api_key": "test"}})
			if err != nil {
				t.Fatal(err)
			}
			if len(array(result["claims"])) != test.expected {
				t.Fatal(result)
			}
			if test.name == "invented quote" && len(f.roles) != 1 {
				t.Fatal("invalid citation sent for review")
			}
		})
	}
}
func TestAdaptiveCallsBounded(t *testing.T) {
	s, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	defer s.DB.Close()
	ctx := context.Background()
	b, _ := s.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
	hit := Object{"kind": "original", "source_id": "paper.pdf", "revision": 1, "page": 1, "quote": "143M parameters"}
	selected := selectEvidence([]Object{hit}, false, false)
	draft := Object{"claims": []Object{{"text": "143M", "citations": []Object{{"evidence_id": selected[0]["evidence_id"], "quote": "143M parameters"}}}}}
	review := Object{"question_relevant": false, "followup_query": "better query", "verdicts": []Object{{"claim_index": 0, "supported": false, "reason": "wrong scope"}}}
	f := &fakeWorker{pool: []Object{hit}, values: []Object{draft, review, draft, review}}
	engine := &Engine{Store: s, Worker: f}
	result, err := engine.Ask(ctx, "fixture", Object{"query": "question", "kb_ids": []string{store.String(b["id"])}, "algorithm": "legacy"}, Object{"llm": Object{"model": "test", "api_key": "test"}})
	if err != nil {
		t.Fatal(err)
	}
	if len(f.roles) != 4 || f.searches != 2 || result["status"] != "no_evidence" {
		t.Fatal(result, f.roles, f.searches)
	}
}
