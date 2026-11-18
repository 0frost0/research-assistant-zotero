package qa

import (
	"context"
	"strings"
	"sync"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func (r *run) original(data, snapshot Object, ids []string, config Object) (Object, error) {
	strategy := store.String(store.Default(data, "strategy", "adaptive"))
	algorithm := store.String(store.Default(data, "algorithm", "hybrid_rerank"))
	if strategy != "adaptive" && data["algorithm"] == nil {
		algorithm = "legacy"
	}
	hybrid := algorithm != "legacy"
	r.trace["policy"] = "zotero-evidence-v3"
	r.trace["strategy"] = strategy
	r.trace["algorithm"] = algorithm
	r.trace["selection"] = "legacy"
	llm, _ := config["llm"].(map[string]any)
	embedding, _ := config["embedding"].(map[string]any)
	pool, evidence, accepted := []Object{}, []Object{}, []Object{}
	warnings, gaps := []string{}, []string{}
	feedback := []Object{}
	status := "no_evidence"
	query, conversation := conversationContext(data["_conversation_history"], r.question)
	r.trace["conversation_turns"] = len(conversation)
	r.trace["conversation_context_used"] = len(conversation) > 0
	searched, used := map[string]bool{}, map[string]bool{}
	rotate := false
	rounds := 1
	if strategy == "adaptive" {
		rounds = 2
	}
	for round := 0; round < rounds; round++ {
		queries := []string{query}
		if round == 0 && strategy == "adaptive" {
			if focused := definitionQuery(r.question); focused != "" && focused != query {
				queries = append(queries, focused)
			}
		}
		for _, searchQuery := range queries {
			if searched[norm(searchQuery)] {
				continue
			}
			hits, err := r.search(searchQuery, ids, hybrid)
			if err != nil {
				return nil, err
			}
			pool = merge(hits, pool)
			searched[norm(searchQuery)] = true
		}
		ranking := pool
		if rotate {
			ranking = []Object{}
			for _, hit := range pool {
				if !used[candidateKey(hit)] {
					ranking = append(ranking, hit)
				}
			}
		}
		rerank := "disabled"
		if algorithm == "hybrid_rerank" {
			ranking, rerank = r.engine.rerank(r.ctx, r.question, ranking, store.String(embedding["api_key"]))
			if rerank != "succeeded" && rerank != "empty" {
				warnings = append(warnings, "精排不可用，保留原始召回排序。")
			}
		}
		evidence = selectEvidence(ranking, false, strategy == "fixed")
		if rotate {
			evidence = selectEvidence(merge(evidence, pool), false, false)
		}
		for _, hit := range evidence {
			used[candidateKey(hit)] = true
		}
		r.trace["steps"] = append(r.trace["steps"].([]Object), Object{"stage": "retrieve", "round": round + 1, "candidates": len(pool), "selected": len(evidence), "rerank": Object{"status": rerank}, "rotated": rotate})
		if store.String(llm["api_key"]) == "" || store.String(llm["model"]) == "" {
			status = "model_unavailable"
			break
		}
		payload := Object{"question": r.question, "previous_feedback": feedback, "evidence": evidence}
		if len(conversation) > 0 {
			payload["conversation"] = conversation
		}
		draft, err := r.call("answer", payload)
		if err != nil {
			status = "unavailable"
			accepted = []Object{}
			warnings = append(warnings, "回答输出不可用；未展示未经核验的结论。")
			break
		}
		candidates := grounded(draft, evidence)
		gaps = list(draft["gaps"])
		next := store.String(draft["followup_query"])
		accepted = []Object{}
		feedback = []Object{}
		relevant := true
		if len(candidates) > 0 {
			// The review receives only the exact cited excerpts, not retrieval context.
			reviewClaims := []Object{}
			for _, claim := range candidates {
				cites := []Object{}
				for _, cite := range array(claim["citations"]) {
					cites = append(cites, Object{"evidence_id": cite["evidence_id"], "quote": cite["support_quote"], "page": cite["page"]})
				}
				reviewClaims = append(reviewClaims, Object{"text": claim["text"], "citations": cites})
			}
			review, err := r.call("review", Object{"question": r.question, "claims": reviewClaims})
			if err != nil {
				status = "unavailable"
				accepted = []Object{}
				warnings = append(warnings, "支持性复核不可用；未展示未经复核的结论。")
				break
			}
			if store.String(review["followup_query"]) != "" {
				next = store.String(review["followup_query"])
			}
			relevant = review["question_relevant"] == true
			for i, claim := range candidates {
				matches := []Object{}
				for _, v := range array(review["verdicts"]) {
					if number(v["claim_index"]) == i {
						matches = append(matches, v)
					}
				}
				if len(matches) == 1 && matches[0]["supported"] == true && relevant {
					accepted = append(accepted, claim)
				} else {
					feedback = append(feedback, Object{"claim": claim["text"], "problem": "支持性复核缺失、重复或未通过"})
				}
			}
			if !relevant {
				gaps = append(gaps, "检索到的内容与问题范围不一致，不能作为该问题的答案。")
			}
			if len(accepted) < len(candidates) {
				gaps = append(gaps, "部分结论未通过支持性复核，已隐藏。")
			}
		}
		if len(candidates) != len(array(draft["claims"])) {
			gaps = append(gaps, "部分引文无效。")
			feedback = append(feedback, Object{"problem": "引用ID或逐字摘录无法匹配，请修正或删除该结论"})
		}
		status = "no_evidence"
		if len(accepted) > 0 {
			status = "answered"
			if len(gaps) > 0 {
				status = "partial"
			}
		}
		r.trace["steps"] = append(r.trace["steps"].([]Object), Object{"stage": "answer_review", "round": round + 1, "draft_claims": len(array(draft["claims"])), "accepted": len(accepted), "question_relevant": relevant})
		rotate = len(array(draft["claims"])) == 0 || len(candidates) > 0 && len(accepted) == 0
		canRotate := false
		for _, p := range pool {
			if !used[candidateKey(p)] {
				canRotate = true
			}
		}
		if status == "answered" || round == rounds-1 || strings.TrimSpace(next) == "" && len(feedback) == 0 && !(rotate && canRotate) {
			break
		}
		bump(r.trace, "correction_rounds", 1)
		if strings.TrimSpace(next) != "" && norm(next) != norm(query) {
			query = strings.TrimSpace(next)
			bump(r.trace, "rewrite_rounds", 1)
		}
	}
	return r.finish(snapshot, status, accepted, evidence, gaps, warnings)
}
func (e *Engine) both(ctx context.Context, connection string, data, config Object) (Object, error) {
	ids := list(data["kb_ids"])
	snapshot, err := e.Store.Snapshot(ctx, ids)
	if err != nil {
		return nil, err
	}
	requests := []Object{clone(data), clone(data)}
	requests[0]["scope"] = "original"
	requests[1]["scope"] = "notes"
	requests[1]["strategy"] = "fixed"
	results := make([]Object, 2)
	errs := make([]error, 2)
	var wg sync.WaitGroup
	for i := range requests {
		wg.Add(1)
		go func(i int) { defer wg.Done(); results[i], errs[i] = e.Ask(ctx, connection, requests[i], config) }(i)
	}
	wg.Wait()
	for _, err := range errs {
		if err != nil {
			return nil, err
		}
	}
	if err = e.Store.AssertCurrent(ctx, snapshot); err != nil {
		return nil, err
	}
	claims, evidence := []Object{}, []Object{}
	warnings, gaps := []string{}, []string{}
	trace := Object{"runtime": "go", "scope": "both", "steps": []Object{}, "calls": []Object{}}
	sections := []Object{}
	for i, result := range results {
		scope := []string{"original", "notes"}[i]
		label := []string{"论文原文依据", "你的个人记录"}[i]
		sections = append(sections, Object{"scope": scope, "label": label, "result": result})
		for _, c := range array(result["claims"]) {
			c = clone(c)
			c["scope"] = scope
			claims = append(claims, c)
		}
		evidence = append(evidence, array(result["evidence"])...)
		warnings = append(warnings, list(result["warnings"])...)
		for _, gap := range list(result["gaps"]) {
			gaps = append(gaps, label+"："+gap)
		}
		t := result["trace"].(map[string]any)
		for _, key := range []string{"model_calls", "retrieval_calls", "rewrite_rounds", "correction_rounds", "input_tokens", "output_tokens", "unknown_usage_calls", "total_tokens"} {
			bump(trace, key, number(t[key]))
		}
		for _, key := range []string{"steps", "calls"} {
			for _, entry := range array(t[key]) {
				entry = clone(entry)
				entry["scope"] = scope
				trace[key] = append(trace[key].([]Object), entry)
			}
		}
	}
	status := "no_evidence"
	if len(claims) > 0 {
		status = "partial"
		if results[0]["status"] == "answered" && results[1]["status"] == "answered" {
			status = "answered"
		}
	} else {
		for _, s := range []string{"unavailable", "timeout", "model_unavailable", "indexing"} {
			if results[0]["status"] == s || results[1]["status"] == s {
				status = s
				break
			}
		}
	}
	return Object{"status": status, "question": data["query"], "scope": "both", "claims": claims, "evidence": evidence, "gaps": gaps, "warnings": warnings, "trace": trace, "sections": sections, "revisions": results[0]["revisions"], "review_label": "原文与个人记录分区回答"}, nil
}
