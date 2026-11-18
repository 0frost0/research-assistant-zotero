package store

import (
	"context"
	"database/sql"
	"sort"
	"strings"

	"github.com/google/uuid"
)

func project(v Object, keys ...string) Object {
	out := Object{}
	for _, k := range keys {
		if x, ok := v[k]; ok {
			out[k] = x
		}
	}
	return out
}
func items(v any) []Object {
	out := []Object{}
	switch list := v.(type) {
	case []Object:
		return list
	case []any:
		for _, x := range list {
			if m, ok := x.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}
func (j *Jobs) Feedback(ctx context.Context, id string, d Object) (Object, error) {
	rating := String(d["rating"])
	if rating != "helpful" && rating != "partial" && rating != "incorrect" && rating != "correct_no_answer" {
		return nil, ErrInvalid
	}
	note, e := Text(Default(d, "note", ""), 1000, true)
	if e != nil {
		return nil, e
	}
	j.mu.Lock()
	defer j.mu.Unlock()
	tx, e := j.DB.BeginTx(ctx, nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	rs, e := rows(ctx, tx, "SELECT * FROM qa_jobs WHERE id=?", id)
	if e != nil {
		return nil, e
	}
	if len(rs) == 0 {
		return nil, sql.ErrNoRows
	}
	r := rs[0]
	if r["state"] != "succeeded" || r["result"] == nil {
		return nil, ErrInvalid
	}
	p, e := Decode(String(r["payload"]))
	if e != nil {
		return nil, e
	}
	result, e := Decode(String(r["result"]))
	if e != nil {
		return nil, e
	}
	evidence := []Object{}
	for _, item := range items(result["evidence"]) {
		evidence = append(evidence, project(item, "evidence_id", "kind", "entity_id", "source_id", "revision", "title", "page", "quote", "url", "source_status", "modality", "visual_asset", "document_version", "retrieval_evidence_id", "evidence_group_id"))
	}
	claims := []Object{}
	for _, c := range items(result["claims"]) {
		citations := []Object{}
		for _, cite := range items(c["citations"]) {
			citations = append(citations, project(cite, "evidence_id", "support_quote", "quote", "source_id", "revision", "page", "url", "kind", "modality", "citation_type", "observation", "observation_source", "visual_asset", "document_version", "retrieval_evidence_id"))
		}
		claims = append(claims, Object{"text": c["text"], "scope": c["scope"], "citations": citations})
	}
	snapshot := Canonical(Object{"status": result["status"], "scope": result["scope"], "revisions": Default(result, "revisions", Object{}), "evidence": evidence, "claims": claims, "gaps": Default(result, "gaps", []any{})})
	if len(snapshot) > 500000 {
		return nil, ErrInvalid
	}
	var exists int
	if e = tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM qa_feedback WHERE job_id=?", id).Scan(&exists); e != nil {
		return nil, e
	}
	if exists > 0 {
		return nil, ErrInvalid
	}
	fid := strings.ReplaceAll(uuid.NewString(), "-", "")[:20]
	stamp := Now()
	hash := Hash([]byte(Canonical(result)))
	_, e = tx.ExecContext(ctx, "INSERT INTO qa_feedback VALUES(?,?,?,?,?,?,?,?,?,?)", fid, id, rating, note, p["query"], Default(p, "scope", "original"), Canonical(Default(p, "kb_ids", []any{})), hash, snapshot, stamp)
	if e != nil {
		return nil, e
	}
	return Object{"feedback_id": fid, "job_id": id, "rating": rating, "note": note, "question": p["query"], "scope": Default(p, "scope", "original"), "result_hash": hash, "created_at": stamp}, tx.Commit()
}
func scopeKey(v any) string {
	ids := []string{}
	switch a := v.(type) {
	case []string:
		ids = append(ids, a...)
	case []any:
		for _, v := range a {
			ids = append(ids, String(v))
		}
	}
	sort.Strings(ids)
	return Canonical(ids)
}
func (j *Jobs) Conversation(ctx context.Context, id string, ids []string) ([]Object, error) {
	out := []Object{}
	if id == "" || len(ids) == 0 {
		return out, nil
	}
	rs, e := rows(ctx, j.DB, "SELECT payload,result FROM qa_jobs WHERE state='succeeded' ORDER BY created_at DESC LIMIT 200")
	if e != nil {
		return nil, e
	}
	for _, r := range rs {
		p, e := Decode(String(r["payload"]))
		if e != nil {
			continue
		}
		if p["conversation_id"] != id || scopeKey(p["kb_ids"]) != scopeKey(ids) {
			continue
		}
		result, e := Decode(String(r["result"]))
		if e != nil {
			continue
		}
		claims := []string{}
		for _, c := range items(result["claims"]) {
			if String(c["text"]) != "" {
				claims = append(claims, String(c["text"]))
			}
		}
		out = append(out, Object{"question": p["query"], "claims": claims})
		if len(out) == 3 {
			break
		}
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, nil
}
func (j *Jobs) Stats(ctx context.Context) (Object, error) {
	rs, e := rows(ctx, j.DB, "SELECT state,payload FROM qa_jobs")
	if e != nil {
		return nil, e
	}
	states := map[string]int{}
	total, success := 0, 0
	for _, r := range rs {
		state := String(r["state"])
		states[state]++
		p, e := Decode(String(r["payload"]))
		if e == nil && p["scope"] == "notes" {
			total++
			if state == "succeeded" {
				success++
			}
		}
	}
	return Object{"total": len(rs), "by_state": states, "notes_total": total, "notes_succeeded": success}, nil
}
func (s *Store) Stats(ctx context.Context) (Object, error) {
	result := Object{}
	for name, query := range map[string]string{
		"knowledge_bases": "SELECT COUNT(*) FROM knowledge_bases", "active_knowledge_bases": "SELECT COUNT(*) FROM knowledge_bases WHERE archived=0",
		"entities": "SELECT COUNT(*) FROM entities", "documents": "SELECT COUNT(*) FROM entities WHERE kind='document'", "external_records": "SELECT COUNT(*) FROM entities WHERE kind IN ('note','annotation')",
		"local_notes": "SELECT COUNT(*) FROM local_notes", "pdf_objects": "SELECT COUNT(*) FROM objects",
	} {
		var n int
		if e := s.DB.QueryRowContext(ctx, query).Scan(&n); e != nil {
			return nil, e
		}
		result[name] = n
	}
	rs, e := rows(ctx, s.DB, "SELECT payload FROM local_notes")
	if e != nil {
		return nil, e
	}
	active := 0
	for _, r := range rs {
		p, e := Decode(String(r["payload"]))
		if e == nil && p["confirmed"] == true && p["archived"] != true {
			active++
		}
	}
	result["active_confirmed_notes"] = active
	return result, nil
}
