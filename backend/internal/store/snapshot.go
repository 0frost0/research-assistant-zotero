package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"unicode"
)

func compact(s string) string {
	return strings.ToLower(strings.Map(func(r rune) rune {
		if unicode.IsSpace(r) {
			return -1
		}
		return r
	}, s))
}
func annotate(ctx context.Context, q queryer, p Object) error {
	anchors, _ := p["anchors"].([]any)
	clean := []any{}
	verifiedAll := len(anchors) > 0
	for _, raw := range anchors {
		a, ok := raw.(map[string]any)
		if !ok {
			verifiedAll = false
			continue
		}
		verified := a["verification_status"] == "verified"
		if a["page"] != nil && strings.HasSuffix(String(a["source"]), ".pdf") {
			var raw string
			e := q.QueryRowContext(ctx, "SELECT pages FROM objects WHERE hash=?", strings.TrimSuffix(String(a["source"]), ".pdf")).Scan(&raw)
			if e != nil && e != sql.ErrNoRows {
				return e
			}
			if e == nil {
				var pages []Object
				if e = json.Unmarshal([]byte(raw), &pages); e != nil {
					return e
				}
				verified = false
				page, _ := Int(a["page"])
				for _, p := range pages {
					if p["page"] == float64(page) && strings.Contains(compact(String(p["text"])), compact(String(a["quote"]))) {
						verified = true
					}
				}
			}
		}
		a["verification_status"] = "unverified"
		if verified {
			a["verification_status"] = "verified"
		}
		verifiedAll = verifiedAll && verified
		clean = append(clean, a)
	}
	p["anchors"] = clean
	p["source_verified"] = verifiedAll
	return nil
}

func (s *Store) Snapshot(ctx context.Context, ids []string) (Object, error) {
	if len(ids) == 0 || len(ids) > 20 {
		return nil, ErrInvalid
	}
	tx, e := s.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	bases := []Object{}
	entities := []Object{}
	notes := []Object{}
	seenB := map[string]bool{}
	seenE := map[string]bool{}
	for _, id := range ids {
		if seenB[id] {
			continue
		}
		seenB[id] = true
		b, e := base(ctx, tx, id)
		if e != nil {
			return nil, e
		}
		if b["archived"] != int64(0) {
			return nil, ErrConflict
		}
		bases = append(bases, b)
		es, e := rows(ctx, tx, "SELECT e.* FROM entities e JOIN memberships m ON e.id=m.entity_id WHERE m.kb_id=?", id)
		if e != nil {
			return nil, e
		}
		for _, entity := range es {
			if seenE[String(entity["id"])] {
				continue
			}
			seenE[String(entity["id"])] = true
			p, e := Decode(String(entity["payload"]))
			if e != nil {
				return nil, e
			}
			if p["kind"] == "document" {
				var raw string
				if e = tx.QueryRowContext(ctx, "SELECT report FROM objects WHERE hash=?", p["hash"]).Scan(&raw); e != nil {
					return nil, e
				}
				r, e := Decode(raw)
				if e != nil {
					return nil, e
				}
				extraction := Object{}
				for _, k := range []string{"total_pages", "native_pages", "low_text_pages", "empty_pages"} {
					extraction[k] = r[k]
				}
				p["extraction"] = extraction
			}
			entity["data"] = p
			entities = append(entities, entity)
		}
		ns, e := rows(ctx, tx, "SELECT * FROM local_notes WHERE kb_id=?", id)
		if e != nil {
			return nil, e
		}
		for _, note := range ns {
			p, e := Decode(String(note["payload"]))
			if e != nil {
				return nil, e
			}
			if e = annotate(ctx, tx, p); e != nil {
				return nil, e
			}
			note["data"] = p
			notes = append(notes, note)
		}
	}
	return Object{"bases": bases, "entities": entities, "notes": notes}, tx.Commit()
}
func (s *Store) AssertCurrent(ctx context.Context, snapshot Object) error {
	for _, b := range snapshot["bases"].([]Object) {
		current, e := s.Base(ctx, String(b["id"]))
		if e != nil {
			return e
		}
		if current["revision"] != b["revision"] {
			return ErrConflict
		}
	}
	return nil
}

// ActiveBaseRevisions reads the selected revisions in one transaction without loading content.
func (s *Store) ActiveBaseRevisions(ctx context.Context, ids []string) (map[string]int64, error) {
	if len(ids) == 0 || len(ids) > 20 {
		return nil, ErrInvalid
	}
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	revisions := make(map[string]int64, len(ids))
	for _, id := range ids {
		if _, seen := revisions[id]; seen {
			continue
		}
		var revision int64
		var archived bool
		err := tx.QueryRowContext(ctx, "SELECT revision, archived FROM knowledge_bases WHERE id=?", id).Scan(&revision, &archived)
		if err != nil {
			return nil, err
		}
		if archived {
			return nil, ErrConflict
		}
		revisions[id] = revision
	}
	return revisions, tx.Commit()
}

func (s *Store) NoteHistory(ctx context.Context, id string) ([]Object, error) {
	tx, e := s.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	var kb string
	if e = tx.QueryRowContext(ctx, "SELECT kb_id FROM local_notes WHERE id=?", id).Scan(&kb); e != nil {
		return nil, e
	}
	rs, e := rows(ctx, tx, "SELECT revision,payload FROM note_revisions WHERE id=? ORDER BY revision DESC", id)
	if e != nil {
		return nil, e
	}
	result := []Object{}
	for _, r := range rs {
		p, e := Decode(String(r["payload"]))
		if e != nil {
			return nil, e
		}
		if e = annotate(ctx, tx, p); e != nil {
			return nil, e
		}
		p["id"] = id
		p["kb_id"] = kb
		p["revision"] = r["revision"]
		result = append(result, p)
	}
	return result, tx.Commit()
}
