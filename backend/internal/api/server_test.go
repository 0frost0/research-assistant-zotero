package api

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/inference"
	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func testServer(t *testing.T) *Server {
	t.Helper()
	s, err := New(t.TempDir(), "test-token", inference.New("http://127.0.0.1:1", "private-token"), "")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(s.Close)
	return s
}
func request(s http.Handler, method, path, body, auth, origin string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "http://127.0.0.1:8776/api/knowledge/"+path, strings.NewReader(body))
	r.Header.Set("Authorization", auth)
	r.Header.Set("Origin", origin)
	r.Header.Set("X-Research-Connection", "fixture")
	w := httptest.NewRecorder()
	s.ServeHTTP(w, r)
	return w
}
func TestKnowledgeWorkbenchSharesGoAPI(t *testing.T) {
	s := testServer(t)
	web := s.Handler(filepath.Join("..", "..", "..", "research_assistant", "web", "static"))
	for path, fragment := range map[string]string{
		"/knowledge": "searchForm", "/static/knowledge.js": "/api/knowledge/",
		"/static/knowledge.css": ".workspace", "/static/vendor/lucide.min.js": "lucide",
	} {
		w := httptest.NewRecorder()
		web.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 || !strings.Contains(w.Body.String(), fragment) || w.Header().Get("Referrer-Policy") != "no-referrer" {
			t.Fatalf("%s: unexpected status or asset headers (%d)", path, w.Code)
		}
	}
	for _, path := range []string{"/static/index.html", "/.data/zotero/connection-token", "/api/status", "/projects"} {
		w := httptest.NewRecorder()
		web.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 404 {
			t.Fatalf("%s: expected 404, got %d", path, w.Code)
		}
	}
	const origin = "http://127.0.0.1:8776"
	const auth = "Bearer test-token"
	if w := request(web, "GET", "status", "", "", ""); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w := request(web, "GET", "status", "", auth, origin); w.Code != 200 || !strings.Contains(w.Body.String(), `"runtime":"go"`) {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, payload := range []string{
		`{"kb_ids":[],"query":"notes"}`,
		`{"kb_ids":["base"],"query":"notes","k":21}`,
		`{"kb_ids":["base"],"query":"notes","scope":"notes","multimodal":true}`,
		`{"kb_ids":["base"],"query":"notes","source_ids":["a","a"]}`,
	} {
		if w := request(web, "POST", "search", payload, auth, origin); w.Code != 400 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	w := request(web, "GET", "export", "", auth, origin)
	if w.Code != 200 {
		t.Fatal(w.Code)
	}
	raw := w.Body.Bytes()
	z, err := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if err != nil || len(z.File) == 0 {
		t.Fatal("workbench export is not a readable ZIP", err)
	}
}

func TestSearchPinsKnowledgeBaseRevisions(t *testing.T) {
	s := testServer(t)
	ctx := context.Background()
	base, err := s.Store.Create(ctx, Object{"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
	if err != nil {
		t.Fatal(err)
	}
	id := store.String(base["id"])
	worker := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body struct {
			Payload           Object           `json:"payload"`
			ExpectedRevisions map[string]int64 `json:"expected_revisions"`
			AllowedSources    []string         `json:"allowed_source_ids"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
			w.WriteHeader(500)
			return
		}
		current, err := s.Store.ActiveBaseRevisions(r.Context(), []string{id})
		if err == nil && body.Payload["query"] == "changed during indexing" && body.ExpectedRevisions[id] < current[id] {
			jsonResponse(w, 409, Object{"error": "revision_conflict"})
			return
		}
		if err != nil || len(body.ExpectedRevisions) != 1 || body.ExpectedRevisions[id] != current[id] {
			t.Errorf("unexpected worker scope: %v, want %v (%v)", body.ExpectedRevisions, current, err)
			w.WriteHeader(500)
			return
		}
		switch body.Payload["query"] {
		case "complete":
			if body.AllowedSources == nil || len(body.AllowedSources) != 0 {
				t.Errorf("expected an explicit empty source list, got %#v", body.AllowedSources)
			}
		case "outside result", "outside indexing result":
			response := Object{"revisions": current, "results": []Object{{"source_id": "notes/outside-1.md"}}}
			if body.Payload["query"] == "outside indexing result" {
				response["status"] = "indexing"
				jsonResponse(w, 202, response)
			} else {
				jsonResponse(w, 200, response)
			}
			return
		case "indexing":
			jsonResponse(w, 202, Object{"status": "indexing", "results": []Object{}})
			return
		case "changed before lookup":
			jsonResponse(w, 409, Object{"error": "revision_conflict"})
			return
		case "changed during lookup", "changed during indexing":
			if _, err := s.Store.SaveNote(r.Context(), id, "", Object{"body": "New note"}); err != nil {
				t.Error(err)
				w.WriteHeader(500)
				return
			}
			if body.Payload["query"] == "changed during indexing" {
				jsonResponse(w, 202, Object{"status": "indexing", "results": []Object{}})
				return
			}
		case "wrong worker revision":
			current[id]++
		}
		jsonResponse(w, 200, Object{"revisions": current, "results": []Object{}})
	}))
	t.Cleanup(worker.Close)
	s.Worker.URL = worker.URL
	for _, tc := range []struct {
		query string
		ids   []string
		want  int
	}{
		{"unknown library", []string{"missing"}, 404},
		{"indexing", []string{id}, 202},
		{"complete", []string{id, id}, 200},
		{"outside result", []string{id}, 503},
		{"outside indexing result", []string{id}, 503},
		{"changed before lookup", []string{id}, 409},
		{"wrong worker revision", []string{id}, 409},
		{"changed during lookup", []string{id}, 409},
		{"complete after retry", []string{id}, 200},
	} {
		payload := store.Canonical(Object{"query": tc.query, "scope": "notes", "kb_ids": tc.ids})
		w := request(s, "POST", "search", payload, "Bearer test-token", "")
		if w.Code != tc.want {
			t.Errorf("%s: got %d, want %d (%s)", tc.query, w.Code, tc.want, w.Body.String())
		}
		if tc.want >= 400 && strings.Contains(w.Body.String(), `"results"`) {
			t.Errorf("%s: error returned results: %s", tc.query, w.Body.String())
		}
	}
	wait, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	if result, err := s.Engine.Worker.Search(wait, "fixture", Object{"query": "changed during indexing", "kb_ids": []string{id}}); !errors.Is(err, store.ErrConflict) || result != nil {
		t.Fatalf("QA search polled across revisions: %v (%v)", result, err)
	}
}

func TestAuthenticationAndRevisionHTTP(t *testing.T) {
	s := testServer(t)
	if w := request(s, "GET", "bases", "", "", ""); w.Code != 401 {
		t.Fatal(w.Code)
	}
	if w := request(s, "GET", "bases", "", "Bearer test-token", "https://other.invalid"); w.Code != 403 {
		t.Fatal(w.Code)
	}
	w := request(s, "POST", "bases", `{"connection":"fixture","library":"user:0","collection":"A","name":"A"}`, "Bearer test-token", "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	base, _ := store.Decode(w.Body.String())
	id := store.String(base["id"])
	w = request(s, "POST", "notes", store.Canonical(Object{"kb_id": id, "body": "first"}), "Bearer test-token", "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	note, _ := store.Decode(w.Body.String())
	w = request(s, "POST", "notes/"+store.String(note["id"]), store.Canonical(Object{"kb_id": id, "body": "stale", "expected_revision": 0}), "Bearer test-token", "")
	if w.Code != 409 {
		t.Fatal(w.Code, w.Body.String())
	}
	for _, body := range []string{`null`, `[]`, `{"kb_ids":["x"],"query":"q","multimodal":"false"}`, `{"kb_ids":["x"],"query":"q","_conversation_history":[]}`, `{"kb_ids":["x"],"query":"q","sufficiency_gate":true}`} {
		if w = request(s, "POST", "ask-async", body, "Bearer test-token", ""); w.Code != 400 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	jobs, err := s.Jobs.List(context.Background())
	if err != nil || len(jobs) != 0 {
		t.Fatal("invalid request created a paid job", jobs, err)
	}
}
func TestConfigKeyPreservation(t *testing.T) {
	s := testServer(t)
	payload := `{"llm":{"base_url":"http://127.0.0.1:8317/v1/chat/completions","model":"go/glm-5.3-flash","api_key":"private-key"},"embedding":{"base_url":"http://127.0.0.1:30001/v1","model":"embedding","api_key":"private-embedding"},"mineru":{"mode":"remote","url":"http://127.0.0.1:30000"}}`
	w := request(s, "POST", "services", payload, "Bearer test-token", "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "private-") {
		t.Fatal("credential returned")
	}
	blank := strings.ReplaceAll(strings.ReplaceAll(payload, "private-key", ""), "private-embedding", "")
	w = request(s, "POST", "services", blank, "Bearer test-token", "")
	if w.Code != 200 {
		t.Fatal(w.Code, w.Body.String())
	}
	config, err := s.config("fixture")
	if err != nil || config["llm"].(map[string]any)["api_key"] != "private-key" {
		t.Fatal("key lost", err)
	}
	path := filepath.Join(s.Store.Home, "service-config.json")
	before, _ := os.ReadFile(path)
	changed := strings.ReplaceAll(blank, "127.0.0.1:8317", "127.0.0.1:9000")
	w = request(s, "POST", "services", changed, "Bearer test-token", "")
	after, _ := os.ReadFile(path)
	if w.Code != 400 || string(before) != string(after) {
		t.Fatal("changed URL reused key or overwrote config")
	}
}
