package store

import (
	"context"
	"database/sql"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
)

func (s *Store) SaveNote(ctx context.Context, kb, id string, d Object) (Object, error) {
	body, e := Text(d["body"], 100000, false)
	if e != nil {
		return nil, e
	}
	title, e := Text(Default(d, "title", ""), 500, true)
	if e != nil {
		return nil, e
	}
	confirmed, ok := Default(d, "confirmed", true).(bool)
	if !ok {
		return nil, ErrInvalid
	}
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		b, e := base(ctx, tx, kb)
		if e != nil {
			return nil, e
		}
		if b["archived"] != int64(0) {
			return nil, ErrConflict
		}
		old := Object{}
		var revision int64
		if id != "" {
			var raw string
			if e = tx.QueryRowContext(ctx, "SELECT payload,revision FROM local_notes WHERE id=? AND kb_id=?", id, kb).Scan(&raw, &revision); e != nil {
				return nil, e
			}
			old, e = Decode(raw)
			if e != nil {
				return nil, e
			}
			expected, ok := Int(d["expected_revision"])
			if !ok || expected != revision {
				return nil, ErrConflict
			}
		}
		kind := Default(d, "type", Default(old, "type", "understanding"))
		if kind != "understanding" && kind != "question" && kind != "idea" {
			return nil, ErrInvalid
		}
		author := Default(d, "author_source", Default(old, "author_source", "user"))
		if author != "user" && author != "ai" || old["author_source"] == "ai" && author != "ai" {
			return nil, ErrInvalid
		}
		archived, ok := Default(d, "archived", Default(old, "archived", false)).(bool)
		if !ok {
			return nil, ErrInvalid
		}
		anchors, ok := Default(d, "anchors", Default(old, "anchors", []any{})).([]any)
		if !ok || len(anchors) > 12 {
			return nil, ErrInvalid
		}
		allowed := map[string]bool{}
		es, e := rows(ctx, tx, "SELECT payload FROM entities e JOIN memberships m ON e.id=m.entity_id WHERE m.kb_id=?", kb)
		if e != nil {
			return nil, e
		}
		for _, entity := range es {
			p, e := Decode(String(entity["payload"]))
			if e != nil {
				return nil, e
			}
			if p["kind"] == "document" {
				allowed[String(p["hash"])+".pdf"] = true
			}
		}
		clean := []any{}
		for _, raw := range anchors {
			a, ok := raw.(map[string]any)
			if !ok {
				return nil, ErrInvalid
			}
			source, e := Text(a["source"], 500, false)
			if e != nil {
				return nil, e
			}
			quote, e := Text(Default(a, "quote", ""), 12000, false)
			if e != nil {
				return nil, e
			}
			if !allowed[source] {
				return nil, ErrInvalid
			}
			if a["page"] != nil {
				page, ok := Int(a["page"])
				if !ok || page < 1 || page > 100000 {
					return nil, ErrInvalid
				}
			}
			if a["url"] != nil {
				u, ok := a["url"].(string)
				if !ok || utf8.RuneCountInString(u) > 2000 || !(strings.HasPrefix(u, "zotero://") || strings.HasPrefix(u, "/")) {
					return nil, ErrInvalid
				}
			}
			kind := String(Default(a, "kind", "original"))
			runes := []rune(kind)
			if len(runes) > 40 {
				kind = string(runes[:40])
			}
			clean = append(clean, Object{"source": source, "quote": quote, "page": a["page"], "url": a["url"], "kind": kind})
		}
		p := Object{"title": title, "body": body, "type": kind, "confirmed": confirmed, "archived": archived, "author_source": author, "confirmed_by": nil, "anchors": clean, "source_status": "unlinked", "updated_at": Now()}
		if confirmed {
			p["confirmed_by"] = "user"
		}
		if len(clean) > 0 {
			p["source_status"] = "linked"
		}
		if e = annotate(ctx, tx, p); e != nil {
			return nil, e
		}
		if author == "ai" && (p["source_verified"] != true || confirmed && old["author_source"] != "ai") {
			return nil, ErrInvalid
		}
		if id == "" {
			id = uuid.NewString()
		}
		revision++
		payload := Canonical(p)
		_, e = tx.ExecContext(ctx, `INSERT INTO local_notes VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision`, id, kb, payload, revision)
		if e != nil {
			return nil, e
		}
		if _, e = tx.ExecContext(ctx, "INSERT INTO note_revisions VALUES(?,?,?)", id, revision, payload); e != nil {
			return nil, e
		}
		if _, e = tx.ExecContext(ctx, "UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", Now(), kb); e != nil {
			return nil, e
		}
		p["id"] = id
		p["kb_id"] = kb
		p["revision"] = revision
		return p, nil
	})
}
