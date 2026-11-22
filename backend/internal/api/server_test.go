package api

import (
	"archive/zip"
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

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
