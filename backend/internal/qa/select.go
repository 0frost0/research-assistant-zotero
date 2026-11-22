package qa

import (
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

var termsRE = regexp.MustCompile(`[a-zA-Z0-9_+.-]+|[\x{4e00}-\x{9fff}]+`)
var chineseRE = regexp.MustCompile(`^[\x{4e00}-\x{9fff}]+$`)

func personal(e Object) bool {
	k := e["kind"]
	return k == "user_note" || k == "external_note" || k == "external_annotation"
}
func relevant(query string, e Object) bool {
	if e["identifier_only"] == true {
		return false
	}
	ignored := map[string]bool{"什么": true, "怎么": true, "如何": true, "哪些": true, "是否": true, "为什么": true, "请问": true, "介绍": true}
	terms := map[string]bool{}
	for _, term := range termsRE.FindAllString(strings.ToLower(query), -1) {
		if ignored[term] {
			continue
		}
		runes := []rune(term)
		if chineseRE.MatchString(term) && len(runes) > 2 {
			for i := 0; i < len(runes)-1; i++ {
				terms[string(runes[i:i+2])] = true
			}
		} else {
			terms[term] = true
		}
	}
	quote := strings.ToLower(store.String(e["quote"]))
	if len(terms) == 0 || strings.TrimSpace(quote) == "" {
		return false
	}
	if len(terms) == 1 {
		if e["score"] == nil {
			return true
		}
		var score float64
		switch n := e["score"].(type) {
		case float64:
			score = n
		default:
			score, _ = numeric(n)
		}
		return score >= .1
	}
	matched := 0
	for t := range terms {
		if strings.Contains(quote, t) {
			matched++
		}
	}
	return float64(matched)/float64(len(terms)) >= .2
}
func candidateKey(e Object) string {
	return store.Canonical([]any{e["source_id"], e["revision"], e["page"], norm(store.String(e["quote"]))})
}
func merge(new, old []Object) []Object {
	out := []Object{}
	seen := map[string]bool{}
	for _, set := range [][]Object{new, old} {
		for _, e := range set {
			k := candidateKey(e)
			if !seen[k] {
				out = append(out, e)
				seen[k] = true
			}
		}
	}
	return out
}
func selectEvidence(pool []Object, memory, fixed bool) []Object {
	out := []Object{}
	size := 0
	seen := map[string]bool{}
	for i, e := range pool {
		if fixed && !memory && i >= 6 {
			break
		}
		if memory && !personal(e) || !memory && e["kind"] != "original" || e["modality"] == "image" {
			continue
		}
		quote := strings.TrimSpace(store.String(e["quote"]))
		if quote == "" || quote == "[图像；无文字图注]" {
			continue
		}
		key := store.Canonical([]any{e["entity_id"], e["revision"]})
		if memory && seen[key] {
			continue
		}
		duplicate := false
		for _, old := range out {
			if !fixed && !memory && old["source_id"] == e["source_id"] && store.Canonical(old["page"]) == store.Canonical(e["page"]) && strings.Contains(norm(store.String(old["quote"])), norm(quote)) {
				duplicate = true
			}
		}
		if duplicate || size+utf8.RuneCountInString(quote) > 12000 {
			continue
		}
		copy := clone(e)
		copy["quote"] = quote
		prefix := "E"
		if memory {
			prefix = "N"
		}
		parts := []any{e["source_id"], e["revision"], e["page"], quote}
		if memory {
			parts = []any{e["source_id"], e["revision"], quote}
		}
		encoded := []string{}
		for _, part := range parts {
			encoded = append(encoded, store.Canonical(part))
		}
		copy["evidence_id"] = prefix + store.Hash([]byte("[" + strings.Join(encoded, ", ") + "]"))[:20]
		out = append(out, copy)
		size += utf8.RuneCountInString(quote)
		seen[key] = true
		if len(out) == 6 {
			break
		}
	}
	return out
}
func sortReranked(pool []Object, scores []float64) []Object {
	type pair struct {
		hit   Object
		score float64
	}
	pairs := make([]pair, len(pool))
	for i, e := range pool {
		pairs[i] = pair{clone(e), scores[i]}
	}
	sort.SliceStable(pairs, func(i, j int) bool { return pairs[i].score > pairs[j].score })
	out := []Object{}
	for _, p := range pairs {
		ranking, _ := p.hit["ranking"].(map[string]any)
		ranking = clone(ranking)
		ranking["rerank_score"] = p.score
		p.hit["ranking"] = ranking
		out = append(out, p.hit)
	}
	return out
}
