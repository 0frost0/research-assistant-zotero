package api

import (
	"context"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/inference"
	"github.com/0frost0/research-assistant-zotero/backend/internal/qa"
	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

type Object = store.Object
type Server struct {
	Store       *store.Store
	Jobs        *store.Jobs
	Engine      *qa.Engine
	Worker      *inference.Retriever
	Root, Token string
	mu          sync.Mutex
	tasks       map[string]context.CancelFunc
	active      map[string]bool
	ctx         context.Context
	cancel      context.CancelFunc
	wg          sync.WaitGroup
	parseGate   chan struct{}
}

func New(root, token string, worker *inference.Client, reranker string) (*Server, error) {
	s, err := store.Open(root)
	if err != nil {
		return nil, err
	}
	jobs, err := store.OpenJobs(filepath.Join(s.Home, "qa_jobs.sqlite3"))
	if err != nil {
		s.DB.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	if err = s.InitParsing(ctx); err != nil {
		cancel()
		jobs.DB.Close()
		s.DB.Close()
		return nil, err
	}
	retriever := &inference.Retriever{Client: worker, Store: s}
	return &Server{Store: s, Jobs: jobs, Engine: &qa.Engine{Store: s, Worker: retriever, Reranker: reranker}, Worker: retriever, Root: root, Token: token, tasks: map[string]context.CancelFunc{}, active: map[string]bool{}, ctx: ctx, cancel: cancel, parseGate: make(chan struct{}, 1)}, nil
}
func (s *Server) Close() { s.cancel(); s.wg.Wait(); s.Jobs.DB.Close(); s.Store.DB.Close() }
func jsonResponse(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}
func fail(w http.ResponseWriter, err error) {
	status := 503
	code := "unavailable"
	if errors.Is(err, store.ErrInvalid) {
		status = 400
		code = "invalid_request"
	}
	if errors.Is(err, store.ErrConflict) {
		status = 409
		code = "revision_conflict"
	}
	if errors.Is(err, sql.ErrNoRows) {
		status = 404
		code = "not_found"
	}
	jsonResponse(w, status, Object{"error": code, "code": code})
}
func (s *Server) config(connection string) (Object, error) {
	raw, err := os.ReadFile(filepath.Join(s.Store.Home, "service-config.json"))
	if os.IsNotExist(err) {
		return Object{}, nil
	}
	if err != nil {
		return nil, err
	}
	all, err := store.Decode(string(raw))
	if err != nil {
		return nil, err
	}
	v, _ := all[connection].(map[string]any)
	if v == nil {
		return Object{}, nil
	}
	return v, nil
}
func publicConfig(config Object) Object {
	out := Object{}
	for _, section := range []string{"llm", "embedding", "mineru"} {
		v, _ := config[section].(map[string]any)
		p := Object{"runtime_connected": nil, "runtime_wired": true}
		if section == "mineru" {
			defaults := Object{"mode": "remote", "url": "", "timeout": 1800, "python": "", "auto_parse": false}
			for k, fallback := range defaults {
				p[k] = store.Default(v, k, fallback)
			}
			p["configured"] = v["mode"] == "local" || v["mode"] == "remote" && store.String(v["url"]) != ""
			p["has_api_key"] = false
		} else {
			p["base_url"] = store.Default(v, "base_url", "")
			p["model"] = store.Default(v, "model", "")
			p["has_api_key"] = store.String(v["api_key"]) != ""
			p["configured"] = p["has_api_key"] == true && p["base_url"] != "" && p["model"] != ""
		}
		if section == "llm" {
			p["supports_images"] = v["supports_images"] == true
		}
		if section == "embedding" {
			p["dimension"] = store.Default(v, "dimension", 2048)
			p["revision"] = store.Default(v, "revision", "1")
			p["protocol"] = "vllm-qwen3-vl-messages-v1"
		}
		out[section] = p
	}
	return out
}
func stringsList(v any) ([]string, error) {
	a, ok := v.([]any)
	if !ok || len(a) < 1 || len(a) > 20 {
		return nil, store.ErrInvalid
	}
	out := []string{}
	for _, x := range a {
		s, ok := x.(string)
		if !ok || s == "" {
			return nil, store.ErrInvalid
		}
		out = append(out, s)
	}
	return out, nil
}

var conversationRE = regexp.MustCompile(`^[A-Za-z0-9_-]{1,80}$`)

func validateSearch(d Object) error {
	if _, err := stringsList(d["kb_ids"]); err != nil {
		return err
	}
	if _, err := store.Text(d["query"], 2000, false); err != nil {
		return err
	}
	scope := store.Default(d, "scope", "both")
	if scope != "original" && scope != "notes" && scope != "both" {
		return store.ErrInvalid
	}
	count, ok := store.Int(store.Default(d, "k", 8))
	if !ok || count < 1 || count > 20 {
		return store.ErrInvalid
	}
	textOnly, textOK := store.Default(d, "text_only", false).(bool)
	visual, visualOK := store.Default(d, "multimodal", false).(bool)
	if !textOK || !visualOK || visual && (textOnly || scope == "notes") {
		return store.ErrInvalid
	}
	mode := store.Default(d, "retrieval_mode", "legacy")
	if mode != "legacy" && (mode != "hybrid" || !textOnly && !visual) {
		return store.ErrInvalid
	}
	if raw := d["source_ids"]; raw != nil {
		ids, err := stringsList(raw)
		if err != nil || len(ids) > 4 {
			return store.ErrInvalid
		}
		seen := map[string]bool{}
		for _, id := range ids {
			if strings.TrimSpace(id) == "" || seen[id] {
				return store.ErrInvalid
			}
			seen[id] = true
		}
	}
	return nil
}

func validateAsk(d Object) error {
	if _, err := stringsList(d["kb_ids"]); err != nil {
		return err
	}
	if _, err := store.Text(d["query"], 2000, false); err != nil {
		return err
	}
	scope := store.Default(d, "scope", "original")
	if scope != "original" && scope != "notes" && scope != "both" {
		return store.ErrInvalid
	}
	strategy := store.Default(d, "strategy", "adaptive")
	if strategy != "fixed" && strategy != "selected" && strategy != "adaptive" {
		return store.ErrInvalid
	}
	algorithm := store.Default(d, "algorithm", "hybrid_rerank")
	if algorithm != "legacy" && algorithm != "hybrid" && algorithm != "hybrid_rerank" {
		return store.ErrInvalid
	}
	// Experimental visual and sufficiency routes are not yet ported. Never silently
	// accept them as text-only execution.
	for _, k := range []string{"multimodal", "sufficiency_gate"} {
		b, ok := store.Default(d, k, false).(bool)
		if !ok || b {
			return store.ErrInvalid
		}
	}
	if v, ok := d["conversation_id"]; ok {
		value, ok := v.(string)
		if !ok || !conversationRE.MatchString(value) {
			return store.ErrInvalid
		}
	}
	for k := range d {
		if strings.HasPrefix(k, "_") {
			return store.ErrInvalid
		}
	}
	return nil
}
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if s.Token == "" {
		jsonResponse(w, 503, Object{"error": "not_configured"})
		return
	}
	if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+s.Token)) != 1 {
		jsonResponse(w, 401, Object{"error": "unauthorized"})
		return
	}
	if origin := r.Header.Get("Origin"); origin != "" && origin != "http://"+r.Host {
		jsonResponse(w, 403, Object{"error": "cross_origin"})
		return
	}
	connection := strings.TrimSpace(r.Header.Get("X-Research-Connection"))
	if len(connection) > 200 {
		fail(w, store.ErrInvalid)
		return
	}
	config, err := s.config(connection)
	if err != nil {
		fail(w, err)
		return
	}
	path := strings.TrimPrefix(r.URL.Path, "/api/knowledge/")
	parts := strings.Split(path, "/")
	if len(parts) > 2 {
		fail(w, sql.ErrNoRows)
		return
	}
	action := parts[0]
	id := ""
	if len(parts) == 2 {
		id = parts[1]
	}
	ctx := r.Context()
	var result any
	status := 200
	if r.Method == "GET" {
		switch action {
		case "status":
			var knowledge, jobs Object
			knowledge, err = s.Store.Stats(ctx)
			if err == nil {
				jobs, err = s.Jobs.Stats(ctx)
			}
			llm, _ := config["llm"].(map[string]any)
			result = Object{"protocol": 1, "runtime": "go", "backend": "multimodal", "generation": store.String(llm["api_key"]) != "", "capabilities": []string{"search", "ask", "memory_ask", "compare"}, "qa_max_calls": 4, "comparison_max_sources": 4, "knowledge": knowledge, "qa_jobs": jobs}
		case "bases":
			if id == "" {
				var bases []Object
				bases, err = s.Store.Bases(ctx)
				result = Object{"bases": bases}
			} else {
				result, err = s.Store.Snapshot(ctx, []string{id})
			}
		case "history":
			result, err = s.Store.NoteHistory(ctx, id)
		case "jobs":
			if id == "" {
				var jobs []Object
				jobs, err = s.Jobs.List(ctx)
				result = Object{"jobs": jobs}
			} else {
				result, err = s.Jobs.Public(ctx, id)
			}
		case "services":
			if connection == "" {
				err = store.ErrInvalid
			} else {
				result = publicConfig(config)
			}
		case "objects":
			var exists bool
			exists, err = s.Store.ObjectExists(ctx, id)
			result = Object{"hash": id, "exists": exists}
		case "export":
			var temp *os.File
			temp, err = os.CreateTemp("", "research-knowledge-backup-*.zip")
			if err == nil {
				name := temp.Name()
				temp.Close()
				defer os.Remove(name)
				err = s.Store.Export(ctx, name)
				if err == nil {
					w.Header().Set("Content-Disposition", `attachment; filename="research-knowledge-backup.zip"`)
					w.Header().Set("Cache-Control", "no-store")
					http.ServeFile(w, r, name)
					return
				}
			}
		case "parsing":
			var jobs []Object
			jobs, err = s.Store.ParsingJobs(ctx)
			result = Object{"jobs": jobs}
		default:
			err = sql.ErrNoRows
		}
	} else if r.Method == "POST" || r.Method == "PUT" {
		if action == "objects" && id != "" && r.Method == "PUT" {
			r.Body = http.MaxBytesReader(w, r.Body, 25*1024*1024)
			content, err := io.ReadAll(r.Body)
			if err != nil || len(content) < 5 || string(content[:5]) != "%PDF-" || store.Hash(content) != id {
				fail(w, store.ErrInvalid)
				return
			}
			exists, err := s.Store.ObjectExists(ctx, id)
			if err != nil {
				fail(w, err)
				return
			}
			if exists {
				jsonResponse(w, 200, Object{"hash": id, "cached": true})
				return
			}
			extracted, err := s.Worker.Call(ctx, connection, "extract", Object{"payload": Object{"hash": id, "content": base64.StdEncoding.EncodeToString(content)}})
			if err != nil {
				fail(w, err)
				return
			}
			result, err := s.Store.PutExtracted(ctx, id, content, extracted)
			if err != nil {
				fail(w, err)
				return
			}
			jsonResponse(w, 200, result)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, 8*1024*1024)
		raw, readErr := io.ReadAll(r.Body)
		if readErr != nil {
			fail(w, store.ErrInvalid)
			return
		}
		d, decodeErr := store.Decode(string(raw))
		if decodeErr != nil {
			fail(w, store.ErrInvalid)
			return
		}
		switch action {
		case "services":
			result, err = s.saveConfig(connection, d)
		case "bases":
			result, err = s.Store.Create(ctx, d)
		case "sync":
			if d["allow_upload"] != true {
				err = store.ErrInvalid
			} else {
				result, err = s.Store.Sync(ctx, id, d)
				mineru, _ := config["mineru"].(map[string]any)
				if err == nil && mineru["auto_parse"] == true {
					parsed, parseErr := s.parse(ctx, connection, []string{id}, config)
					if parseErr != nil {
						err = parseErr
					} else {
						result.(map[string]any)["parsing_jobs"] = parsed["jobs"]
					}
				}
			}
		case "parse":
			var ids []string
			ids, err = stringsList(d["kb_ids"])
			if err == nil {
				result, err = s.parse(ctx, connection, ids, config)
			}
		case "archive":
			result, err = s.Store.Archive(ctx, id, d)
		case "notes":
			result, err = s.Store.SaveNote(ctx, store.String(d["kb_id"]), id, d)
		case "search":
			if err = validateSearch(d); err == nil {
				var search Object
				search, err = s.Worker.SearchOnce(ctx, connection, d)
				if search["status"] == "indexing" {
					status = 202
				}
				result = search
			}
		case "ask", "ask-async":
			err = validateAsk(d)
			if err == nil {
				if action == "ask" {
					result, err = s.ask(ctx, connection, d, config)
				} else {
					var job Object
					job, err = s.Jobs.Create(ctx, d)
					if err == nil {
						s.start(store.String(job["job_id"]), connection, d, config)
						result = job
						status = 202
					}
				}
			}
		case "compare":
			s.mu.Lock()
			busy := s.active["compare"]
			if !busy {
				s.active["compare"] = true
			}
			s.mu.Unlock()
			if busy {
				err = store.ErrInvalid
			} else {
				result, err = s.Engine.Compare(ctx, connection, d)
				s.mu.Lock()
				delete(s.active, "compare")
				s.mu.Unlock()
			}
		case "jobs":
			switch d["action"] {
			case "cancel":
				result, err = s.Jobs.Cancel(ctx, id)
				if err == nil {
					s.mu.Lock()
					if cancel := s.tasks[id]; cancel != nil {
						cancel()
					}
					s.mu.Unlock()
				}
			case "retry":
				var job Object
				job, err = s.Jobs.Retry(ctx, id)
				if err == nil {
					var payload Object
					payload, err = s.Jobs.Payload(ctx, store.String(job["job_id"]))
					if err == nil {
						s.start(store.String(job["job_id"]), connection, payload, config)
						result = job
						status = 202
					}
				}
			case "feedback":
				result, err = s.Jobs.Feedback(ctx, id, d)
				status = 201
			default:
				err = store.ErrInvalid
			}
		default:
			err = sql.ErrNoRows
		}
	} else {
		jsonResponse(w, 405, Object{"error": "method_not_allowed"})
		return
	}
	if err != nil {
		fail(w, err)
		return
	}
	jsonResponse(w, status, result)
}
func (s *Server) ask(ctx context.Context, connection string, data, config Object) (Object, error) {
	scope := store.String(store.Default(data, "scope", "original"))
	s.mu.Lock()
	if s.active[scope] {
		s.mu.Unlock()
		return nil, errors.New("qa_busy")
	}
	s.active[scope] = true
	s.mu.Unlock()
	defer func() { s.mu.Lock(); delete(s.active, scope); s.mu.Unlock() }()
	if conversation := store.String(data["conversation_id"]); conversation != "" {
		ids, err := stringsList(data["kb_ids"])
		if err != nil {
			return nil, err
		}
		history, err := s.Jobs.Conversation(ctx, conversation, ids)
		if err != nil {
			return nil, err
		}
		copy := Object{}
		for k, v := range data {
			copy[k] = v
		}
		copy["_conversation_history"] = history
		data = copy
	}
	return s.Engine.Ask(ctx, connection, data, config)
}
func (s *Server) start(id, connection string, data, config Object) {
	ctx, cancel := context.WithCancel(s.ctx)
	s.mu.Lock()
	s.tasks[id] = cancel
	s.mu.Unlock()
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		defer cancel()
		defer func() { s.mu.Lock(); delete(s.tasks, id); s.mu.Unlock() }()
		if err := s.Jobs.MarkRunning(ctx, id); err != nil {
			return
		}
		result, err := s.ask(ctx, connection, data, config)
		finishCtx, done := context.WithTimeout(context.Background(), 5*time.Second)
		defer done()
		state := "succeeded"
		var message any
		if err != nil {
			state = "failed"
			message = "问答未完成，请检查服务后重新提交。"
			if ctx.Err() != nil {
				state = "interrupted"
				message = "服务停止中断，未自动重试。"
			}
		}
		s.Jobs.Finish(finishCtx, id, state, result, message)
	}()
}
