package qa

import (
	"encoding/json"
	"strings"
	"unicode"
	"unicode/utf8"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

type Object = store.Object

func array(v any) []Object {
	out := []Object{}
	switch a := v.(type) {
	case []Object:
		return a
	case []any:
		for _, v := range a {
			if m, ok := v.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}
func list(v any) []string {
	out := []string{}
	switch a := v.(type) {
	case []string:
		return a
	case []any:
		for _, v := range a {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
	}
	return out
}
func norm(s string) string {
	return strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, s)
}
func clone(v Object) Object {
	n := Object{}
	for k, x := range v {
		n[k] = x
	}
	return n
}
func only(v Object, keys ...string) bool {
	allowed := map[string]bool{}
	for _, k := range keys {
		allowed[k] = true
	}
	for k := range v {
		if !allowed[k] {
			return false
		}
	}
	return true
}
func text(v any, min, max int) bool {
	s, ok := v.(string)
	return ok && utf8.RuneCountInString(s) >= min && utf8.RuneCountInString(s) <= max
}
func stringsField(v Object, key string, maxCount, maxLength int) bool {
	raw, ok := v[key]
	if !ok {
		return true
	}
	a, ok := raw.([]any)
	if !ok || len(a) > maxCount {
		return false
	}
	for _, s := range a {
		if !text(s, 0, maxLength) {
			return false
		}
	}
	return true
}
func validDraft(v Object, memory bool) bool {
	if v == nil || !only(v, "claims", "gaps", "followup_query", "question_relevant") {
		return false
	}
	if memory {
		if _, ok := v["question_relevant"].(bool); !ok {
			return false
		}
	} else {
		if _, ok := v["question_relevant"]; ok {
			return false
		}
	}
	if !stringsField(v, "gaps", 6, 12000) {
		return false
	}
	if q, ok := v["followup_query"]; ok && !text(q, 0, 300) {
		return false
	}
	raw, ok := v["claims"]
	if !ok {
		return true
	}
	claims, ok := raw.([]any)
	if !ok || len(claims) > 6 {
		return false
	}
	for _, raw := range claims {
		c, ok := raw.(map[string]any)
		if !ok || !only(c, "text", "citations") || !text(c["text"], 1, 1200) {
			return false
		}
		cites, ok := c["citations"].([]any)
		if !ok || len(cites) < 1 || len(cites) > 4 {
			return false
		}
		for _, raw := range cites {
			cite, ok := raw.(map[string]any)
			if !ok || !only(cite, "evidence_id", "quote") || !text(cite["evidence_id"], 0, 200) || !text(cite["quote"], 1, 1000) {
				return false
			}
		}
	}
	return true
}
func validReview(v Object) bool {
	if v == nil || !only(v, "verdicts", "followup_query", "question_relevant") {
		return false
	}
	if _, ok := v["question_relevant"].(bool); !ok {
		return false
	}
	if q, ok := v["followup_query"]; ok && !text(q, 0, 300) {
		return false
	}
	raw, ok := v["verdicts"]
	if !ok {
		return true
	}
	vs, ok := raw.([]any)
	if !ok || len(vs) > 6 {
		return false
	}
	for _, raw := range vs {
		v, ok := raw.(map[string]any)
		if !ok || !only(v, "claim_index", "supported", "reason") {
			return false
		}
		n, ok := store.Int(v["claim_index"])
		if !ok || n < 0 {
			return false
		}
		if _, ok = v["supported"].(bool); !ok || !text(v["reason"], 0, 500) {
			return false
		}
	}
	return true
}
func grounded(draft Object, evidence []Object) []Object {
	known := map[string]Object{}
	for _, e := range evidence {
		known[store.String(e["evidence_id"])] = e
	}
	out := []Object{}
	for _, claim := range array(draft["claims"]) {
		cites := []Object{}
		valid := true
		for _, c := range array(claim["citations"]) {
			e, ok := known[store.String(c["evidence_id"])]
			q := strings.TrimSpace(store.String(c["quote"]))
			if !ok || utf8.RuneCountInString(norm(q)) < 8 || !strings.Contains(norm(store.String(e["quote"])), norm(q)) {
				valid = false
				break
			}
			copy := clone(e)
			copy["support_quote"] = q
			cites = append(cites, copy)
		}
		if valid && len(cites) > 0 {
			out = append(out, Object{"text": claim["text"], "citations": cites})
		}
	}
	return out
}
func number(v any) int                 { n, _ := store.Int(v); return int(n) }
func bump(v Object, key string, n int) { v[key] = number(v[key]) + n }
func asJSON(v Object) Object {
	raw, _ := json.Marshal(v)
	out, _ := store.Decode(string(raw))
	return out
}
