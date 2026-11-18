package qa

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func comparisonGrounded(claims []Object, evidence map[string]Object, minimum int) []Object {
	out := []Object{}
	for _, c := range claims {
		ids := list(c["evidence_ids"])
		if len(ids) < 1 || len(ids) > 8 || !text(c["aspect"], 1, 100) || !text(c["statement"], 1, 1200) {
			continue
		}
		sources := map[string]bool{}
		valid := true
		for _, id := range ids {
			e, ok := evidence[id]
			if !ok {
				valid = false
				break
			}
			sources[store.String(e["source"])] = true
		}
		if minimum == 2 && c["relation"] != "agreement" && c["relation"] != "difference" && c["relation"] != "incomparable" {
			valid = false
		}
		if valid && len(sources) >= minimum {
			out = append(out, c)
		}
	}
	return out
}
func (e *Engine) Compare(ctx context.Context, connection string, d Object) (Object, error) {
	if d["allow_external"] != true {
		return nil, store.ErrInvalid
	}
	question, err := store.Text(d["question"], 3000, false)
	if err != nil {
		return nil, err
	}
	ids := list(d["kb_ids"])
	sources := list(d["sources"])
	if len(sources) < 2 || len(sources) > 4 {
		return nil, store.ErrInvalid
	}
	seen := map[string]bool{}
	for _, s := range sources {
		if seen[s] || s == "" {
			return nil, store.ErrInvalid
		}
		seen[s] = true
	}
	count, ok := store.Int(store.Default(d, "k", 5))
	if !ok || count < 1 || count > 8 {
		return nil, store.ErrInvalid
	}
	snapshot, err := e.Store.Snapshot(ctx, ids)
	if err != nil {
		return nil, err
	}
	allowed := map[string]Object{}
	for _, entity := range snapshot["entities"].([]Object) {
		data := entity["data"].(map[string]any)
		if data["kind"] == "document" {
			allowed[store.String(data["hash"])+".pdf"] = data
		}
	}
	for _, s := range sources {
		if allowed[s] == nil {
			return nil, store.ErrInvalid
		}
	}
	ctx, cancel := context.WithTimeout(ctx, 180*time.Second)
	defer cancel()
	documents := []Object{}
	evidence := map[string]Object{}
	titles, urls := Object{}, Object{}
	for i, source := range sources {
		found, err := e.Worker.Search(ctx, connection, Object{"query": question, "scope": "original", "k": count, "kb_ids": ids, "source_ids": []string{source}})
		if err != nil {
			return nil, err
		}
		entries := []Object{}
		remaining := 6000
		for j, hit := range array(found["results"]) {
			if hit["source_id"] != source || hit["modality"] == "image" || hit["modality"] == "multimodal" || hit["content_type"] == "image" || hit["content_type"] == "chart" || hit["quote"] == "[图像；无文字图注]" {
				continue
			}
			quote := []rune(store.String(hit["quote"]))
			length := len(quote)
			if length > 1800 {
				length = 1800
			}
			if length > remaining {
				length = remaining
			}
			if length == 0 {
				continue
			}
			remaining -= length
			ref := fmt.Sprintf("D%dE%d", i+1, j+1)
			item := Object{"ref": ref, "source": source, "page": hit["page"], "quote": string(quote[:length]), "evidence_id": hit["retrieval_evidence_id"], "context_evidence_ids": hit["context_evidence_ids"], "modality": "text", "backend": store.Default(hit, "backend", "multimodal"), "quality_warnings": []string{}, "context_span": nil, "truncated": length < len(quote), "title": allowed[source]["title"], "url": hit["url"]}
			entries = append(entries, item)
			if strings.TrimSpace(store.String(item["quote"])) != "" {
				evidence[ref] = item
			}
			if hit["url"] != nil {
				urls[ref] = hit["url"]
			}
		}
		var message any
		if len(entries) == 0 {
			message = "当前问题未检索到这篇论文的文字证据。"
		}
		documents = append(documents, Object{"source": source, "title": allowed[source]["title"], "evidence": entries, "error": message})
		titles[source] = allowed[source]["title"]
	}
	var calls atomic.Int32
	call := func(role string, payload Object) (Object, error) {
		calls.Add(1)
		callCtx, stop := context.WithTimeout(ctx, 35*time.Second)
		defer stop()
		result, err := e.Worker.Call(callCtx, connection, "comparison-model", Object{"role": role, "payload": payload})
		if err != nil {
			return nil, err
		}
		v, ok := result["value"].(map[string]any)
		if !ok || result["invalid"] == true {
			return nil, store.ErrInvalid
		}
		return v, nil
	}
	analyses := make([]Object, len(documents))
	var wg sync.WaitGroup
	for i, doc := range documents {
		wg.Add(1)
		go func(i int, doc Object) {
			defer wg.Done()
			source := store.String(doc["source"])
			result := Object{"source": source, "status": "no_evidence", "findings": []Object{}, "gaps": []string{}}
			defer func() { analyses[i] = result }()
			if doc["error"] != nil {
				result["gaps"] = []string{store.String(doc["error"])}
				return
			}
			local := map[string]Object{}
			for id, item := range evidence {
				if item["source"] == source {
					local[id] = item
				}
			}
			if len(local) == 0 {
				return
			}
			draft, err := call("analyst", Object{"question": question, "document": doc})
			if err != nil {
				result["status"] = "failed"
				result["gaps"] = []string{"单篇分析未完成，保留检索证据。"}
				return
			}
			findings := comparisonGrounded(array(draft["findings"]), local, 1)
			result["findings"] = findings
			result["gaps"] = list(draft["gaps"])
			if len(findings) > 0 {
				result["status"] = "ok"
			}
		}(i, doc)
	}
	wg.Wait()
	warnings := []string{}
	good := []Object{}
	goodSources := map[string]bool{}
	for _, a := range analyses {
		if a["status"] == "ok" {
			good = append(good, a)
			goodSources[store.String(a["source"])] = true
		}
	}
	candidates, profiles, reviews := []Object{}, []Object{}, []Object{}
	if len(good) >= 2 {
		sort.Slice(good, func(i, j int) bool { return store.String(good[i]["source"]) < store.String(good[j]["source"]) })
		available := map[string]Object{}
		entries := []Object{}
		for _, doc := range documents {
			for _, item := range array(doc["evidence"]) {
				if goodSources[store.String(item["source"])] && evidence[store.String(item["ref"])] != nil {
					available[store.String(item["ref"])] = item
					entries = append(entries, item)
				}
			}
		}
		draft, err := call("synthesizer", Object{"question": question, "analyses": good, "evidence": entries})
		if err != nil {
			warnings = append(warnings, "综合未完成，保留单篇分析。")
		} else {
			candidates = comparisonGrounded(array(draft["comparisons"]), available, 2)
			for _, p := range array(draft["profiles"]) {
				source := store.String(p["source"])
				refs := list(p["evidence_ids"])
				valid := goodSources[source] && len(refs) > 0
				for _, ref := range refs {
					if available[ref] == nil || available[ref]["source"] != source {
						valid = false
					}
				}
				if valid {
					profiles = append(profiles, p)
				}
			}
		}
	} else {
		warnings = append(warnings, "不足两篇文献取得有效单篇分析，未生成跨文献结论。")
	}
	if len(candidates) > 0 {
		entries := []Object{}
		for _, doc := range documents {
			for _, item := range array(doc["evidence"]) {
				if evidence[store.String(item["ref"])] != nil {
					entries = append(entries, item)
				}
			}
		}
		checked, err := call("reviewer", Object{"question": question, "comparisons": candidates, "evidence": entries})
		if err != nil {
			warnings = append(warnings, "复核未完成，候选比较不作为已支持结论。")
		} else {
			reviews = array(checked["verdicts"])
		}
	}
	accepted, withheld := []Object{}, []Object{}
	for i, claim := range candidates {
		matches := []Object{}
		for _, v := range reviews {
			index, ok := store.Int(v["claim_index"])
			if ok && index == int64(i) {
				matches = append(matches, v)
			}
		}
		verdict := Object{"verdict": "uncertain", "reason": "复核项缺失或重复。"}
		if len(matches) == 1 {
			verdict = matches[0]
		}
		row := clone(claim)
		row["review"] = verdict
		if verdict["verdict"] == "supported" {
			accepted = append(accepted, row)
		} else {
			withheld = append(withheld, row)
		}
	}
	if err = e.Store.AssertCurrent(ctx, snapshot); err != nil {
		return nil, err
	}
	status := "ok"
	if len(good) != len(analyses) || len(warnings) > 0 || len(withheld) > 0 || len(accepted) == 0 {
		status = "degraded"
	}
	rounds := 0
	if len(candidates) > 0 {
		rounds = 1
	}
	return Object{"summary": fmt.Sprintf("得到 %d 项通过单轮 AI 复核的比较，%d 项未通过或待核实。", len(accepted), len(withheld)), "status": status, "analyses": analyses, "profiles": profiles, "comparisons": accepted, "withheld": withheld, "documents": documents, "warnings": warnings, "calls": calls.Load(), "call_budget": len(documents) + 2, "review_rounds": rounds, "analyst_concurrency": 4, "source_titles": titles, "source_urls": urls, "limitations": []string{"仅分析当前问题命中的片段，不是全文系统综述。", "单篇分析与条件摘要为 AI 草稿；AI 复核不是人工核验。", "仅发送文字，未进行图片像素推理。"}, "telemetry": Object{"runtime": "go", "model_calls": calls.Load()}}, nil
}
