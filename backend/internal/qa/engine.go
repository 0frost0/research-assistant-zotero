package qa

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
	"github.com/google/uuid"
)

type Worker interface {
	Call(context.Context, string, string, Object) (Object, error)
	Search(context.Context, string, Object) (Object, error)
}
type Engine struct {
	Store    *store.Store
	Worker   Worker
	Reranker string
}

func strconvFloat(v any) (float64, error) { return strconv.ParseFloat(store.String(v), 64) }
func numeric(v any) (float64, error) {
	switch n := v.(type) {
	case json.Number:
		return n.Float64()
	case float64:
		return n, nil
	default:
		return strconvFloat(v)
	}
}
func (e *Engine) rerank(ctx context.Context, question string, pool []Object, key string) ([]Object, string) {
	if e.Reranker == "" || key == "" {
		return pool, "not_configured"
	}
	if len(pool) == 0 {
		return pool, "empty"
	}
	if len(pool) > 40 {
		pool = pool[:40]
	}
	docs := []string{}
	for _, p := range pool {
		q := store.String(p["quote"])
		if utf8.RuneCountInString(q) > 2000 || q == "" {
			return pool, "fallback"
		}
		docs = append(docs, q)
	}
	body := Object{"model": "Qwen/Qwen3-Reranker-0.6B", "query": question, "documents": docs, "instruction": "Retrieve passages that directly answer the question, preserving its conditions, entities and numbers. For a definition question, prefer a passage explaining what the requested concept is, not a passage that merely mentions it."}
	ctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, "POST", e.Reranker, bytes.NewBufferString(store.Canonical(body)))
	if err != nil {
		return pool, "fallback"
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+key)
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	response, err := client.Do(req)
	if err != nil {
		return pool, "fallback"
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return pool, "fallback"
	}
	raw, err := io.ReadAll(io.LimitReader(response.Body, 65537))
	if err != nil || len(raw) > 65536 {
		return pool, "fallback"
	}
	r, err := store.Decode(string(raw))
	if err != nil || r["model"] != "Qwen/Qwen3-Reranker-0.6B" || r["revision"] != "e61197ed45024b0ed8a2d74b80b4d909f1255473" {
		return pool, "fallback"
	}
	values, ok := r["scores"].([]any)
	if !ok || len(values) != len(pool) {
		return pool, "fallback"
	}
	scores := []float64{}
	for _, v := range values {
		n, err := numeric(v)
		if err != nil || math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || n > 1 {
			return pool, "fallback"
		}
		scores = append(scores, n)
	}
	return sortReranked(pool, scores), "succeeded"
}

type run struct {
	engine                      *Engine
	ctx                         context.Context
	connection, question, scope string
	trace                       Object
	started                     time.Time
	calls, max                  int
}

func (r *run) call(role string, payload Object) (Object, error) {
	if r.calls >= r.max {
		return nil, errors.New("call_budget")
	}
	r.calls++
	bump(r.trace, "model_calls", 1)
	bump(r.trace, "unknown_usage_calls", 1)
	start := time.Now()
	entry := Object{"role": role, "status": "started"}
	r.trace["calls"] = append(r.trace["calls"].([]Object), entry)
	ctx, cancel := context.WithTimeout(r.ctx, 45*time.Second)
	defer cancel()
	result, err := r.engine.Worker.Call(ctx, r.connection, "model", Object{"role": role, "payload": payload, "timeout": 45})
	defer func() { entry["elapsed_ms"] = time.Since(start).Milliseconds() }()
	if err != nil {
		entry["status"] = "error"
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			entry["status"] = "timeout"
		}
		return nil, err
	}
	usage, _ := result["usage"].(map[string]any)
	in, ok1 := store.Int(usage["input_tokens"])
	out, ok2 := store.Int(usage["output_tokens"])
	if ok1 && ok2 && in >= 0 && out >= 0 {
		bump(r.trace, "input_tokens", int(in))
		bump(r.trace, "output_tokens", int(out))
		bump(r.trace, "unknown_usage_calls", -1)
		entry["input_tokens"] = in
		entry["output_tokens"] = out
		entry["usage"] = "known"
	} else {
		entry["usage"] = "unknown"
	}
	v, _ := result["value"].(map[string]any)
	valid := false
	switch role {
	case "answer":
		valid = validDraft(v, r.scope == "notes")
	case "review":
		valid = validReview(v)
	case "rewrite":
		valid = v != nil && only(v, "query") && text(store.Default(v, "query", ""), 0, 300)
	}
	if !valid {
		entry["status"] = "invalid_output"
		r.trace["model_output_failure"] = "schema_validation"
		return nil, errors.New("invalid_model_output")
	}
	entry["status"] = "succeeded"
	return v, nil
}
func (r *run) search(query string, ids []string, hybrid bool) ([]Object, error) {
	bump(r.trace, "retrieval_calls", 1)
	p := Object{"query": query, "kb_ids": ids, "scope": r.scope, "k": 20}
	if r.scope == "original" {
		p["text_only"] = true
		p["retrieval_mode"] = "legacy"
		if hybrid {
			p["retrieval_mode"] = "hybrid"
		}
	}
	found, err := r.engine.Worker.Search(r.ctx, r.connection, p)
	if err != nil {
		return nil, err
	}
	r.trace["retrieval_backend"] = found["backend"]
	return array(found["results"]), nil
}
func newRun(e *Engine, ctx context.Context, connection, question, scope, model string, max int) *run {
	return &run{engine: e, ctx: ctx, connection: connection, question: question, scope: scope, started: time.Now(), max: max, trace: Object{"run_id": uuid.NewString(), "model": model, "review_model": model, "model_calls": 0, "retrieval_calls": 0, "rewrite_rounds": 0, "correction_rounds": 0, "input_tokens": 0, "output_tokens": 0, "unknown_usage_calls": 0, "steps": []Object{}, "calls": []Object{}, "runtime": "go", "scope": scope}}
}
func (r *run) finish(snapshot Object, status string, claims, evidence []Object, gaps, warnings []string) (Object, error) {
	if err := r.engine.Store.AssertCurrent(r.ctx, snapshot); err != nil {
		return nil, err
	}
	r.trace["elapsed_ms"] = time.Since(r.started).Milliseconds()
	r.trace["total_tokens"] = number(r.trace["input_tokens"]) + number(r.trace["output_tokens"])
	revisions := Object{}
	for _, b := range snapshot["bases"].([]Object) {
		revisions[store.String(b["id"])] = b["revision"]
	}
	label := "AI 支持性复核，仍需核对原文"
	if r.scope == "notes" {
		label = "回答来自你的个人记录；不代表论文原文结论。"
	}
	return Object{"status": status, "question": r.question, "scope": r.scope, "claims": claims, "evidence": evidence, "gaps": gaps, "warnings": warnings, "trace": r.trace, "revisions": revisions, "review_label": label}, nil
}
func (e *Engine) Ask(ctx context.Context, connection string, data, config Object) (Object, error) {
	ctx, cancel := context.WithTimeout(ctx, 180*time.Second)
	defer cancel()
	scope := store.String(store.Default(data, "scope", "original"))
	if scope == "both" {
		return e.both(ctx, connection, data, config)
	}
	ids := list(data["kb_ids"])
	snapshot, err := e.Store.Snapshot(ctx, ids)
	if err != nil {
		return nil, err
	}
	llm, _ := config["llm"].(map[string]any)
	r := newRun(e, ctx, connection, store.String(data["query"]), scope, store.String(llm["model"]), 4)
	if scope == "notes" {
		r.max = 2
		return r.memory(data, snapshot, ids, llm)
	}
	return r.original(data, snapshot, ids, config)
}
func (r *run) memory(data, snapshot Object, ids []string, llm Object) (Object, error) {
	r.trace["policy"] = "personal-memory-v2"
	r.trace["memory_gate"] = Object{"policy": "char_ngram_coverage", "min_coverage": .2}
	warnings, gaps := []string{}, []string{}
	claims := []Object{}
	status := "no_evidence"
	pool, err := r.search(r.question, ids, false)
	if err != nil {
		return nil, err
	}
	filter := func(q string, pool []Object) []Object {
		out := []Object{}
		for _, e := range pool {
			if relevant(q, e) || q != r.question && relevant(r.question, e) {
				out = append(out, e)
			}
		}
		return selectEvidence(out, true, false)
	}
	evidence := filter(r.question, pool)
	configured := llm["api_key"] != "" && llm["api_key"] != nil && llm["model"] != "" && llm["model"] != nil
	if len(evidence) == 0 && len(pool) > 0 && configured && data["strategy"] != "fixed" {
		candidates := []Object{}
		for _, p := range pool {
			if personal(p) && len(candidates) < 6 {
				candidates = append(candidates, Object{"title": p["title"], "quote": p["quote"], "revision": p["revision"]})
			}
		}
		if len(candidates) > 0 {
			v, e := r.call("rewrite", Object{"question": r.question, "memory_mode": true, "candidates": candidates})
			if e == nil {
				q := strings.TrimSpace(store.String(v["query"]))
				if q != "" && norm(q) != norm(r.question) {
					r.trace["rewrite_rounds"] = 1
					pool, e = r.search(q, ids, false)
					if e != nil {
						return nil, e
					}
					evidence = filter(q, pool)
					warnings = append(warnings, "初次词面检索未命中，已执行一次受限问题改写。")
				}
			} else {
				warnings = append(warnings, "个人记录问题改写失败，未把未核验内容当作答案。")
			}
		}
	}
	if len(evidence) == 0 {
		gaps = append(gaps, "未找到当前有效、已确认的个人记录。")
	} else if !configured {
		status = "model_unavailable"
	} else {
		v, e := r.call("answer", Object{"question": r.question, "memory_mode": true, "evidence": evidence})
		if e != nil {
			status = "unavailable"
			gaps = append(gaps, "模型不可用或输出未通过记录引用核对。")
		} else {
			gaps = list(v["gaps"])
			r.trace["question_relevant"] = v["question_relevant"]
			if v["question_relevant"] == true {
				claims = grounded(v, evidence)
			}
			if len(claims) > 0 {
				status = "answered"
				if len(gaps) > 0 {
					status = "partial"
				}
			}
		}
	}
	return r.finish(snapshot, status, claims, evidence, gaps, warnings)
}
