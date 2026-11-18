package store

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"regexp"
	"sort"
)

var validHash = regexp.MustCompile(`^[0-9a-f]{64}$`)

func (s *Store) ObjectExists(ctx context.Context, sha string) (bool, error) {
	if !validHash.MatchString(sha) {
		return false, ErrInvalid
	}
	var n int
	e := s.DB.QueryRowContext(ctx, "SELECT COUNT(*) FROM objects WHERE hash=?", sha).Scan(&n)
	if e != nil {
		return false, e
	}
	if n == 0 {
		return false, nil
	}
	b, e := os.ReadFile(filepath.Join(s.Home, "objects", sha+".pdf"))
	if os.IsNotExist(e) {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	return Hash(b) == sha, nil
}
func (s *Store) normalize(ctx context.Context, v Object) (Object, error) {
	k, e := Key(v["key"])
	if e != nil {
		return nil, e
	}
	t, e := Text(Default(v, "title", ""), 1000, true)
	if e != nil {
		return nil, e
	}
	kind := String(v["kind"])
	r := Object{"key": k, "kind": kind, "title": t, "parent_key": nil}
	if p := v["parent_key"]; p != nil && p != "" {
		r["parent_key"], e = Key(p)
		if e != nil {
			return nil, e
		}
	}
	if kind == "document" {
		sha := String(v["hash"])
		ok, e := s.ObjectExists(ctx, sha)
		if e != nil {
			return nil, e
		}
		if !ok {
			return nil, ErrInvalid
		}
		r["hash"] = sha
	} else if kind == "note" || kind == "annotation" {
		for field, limit := range map[string]int{"body": 100000, "comment": 20000} {
			r[field], e = Text(Default(v, field, ""), limit, true)
			if e != nil {
				return nil, e
			}
		}
		r["confirmed"] = Default(v, "confirmed", true) == true
		r["author_source"] = "external_zotero"
		if kind == "annotation" {
			r["attachment_key"], e = Key(v["attachment_key"])
			if e != nil {
				return nil, e
			}
			page, ok := Int(v["page"])
			if !ok || page < 1 || page > 100000 {
				return nil, ErrInvalid
			}
			r["page"] = page
		}
	} else {
		return nil, ErrInvalid
	}
	return r, nil
}

func (s *Store) Sync(ctx context.Context, id string, d Object) (Object, error) {
	if d["complete"] != true {
		return nil, ErrInvalid
	}
	raw, ok := d["entities"].([]any)
	if !ok || len(raw) > 5000 {
		return nil, ErrInvalid
	}
	entities := make([]Object, 0, len(raw))
	seen := map[string]bool{}
	for _, v := range raw {
		m, ok := v.(map[string]any)
		if !ok {
			return nil, ErrInvalid
		}
		n, e := s.normalize(ctx, m)
		if e != nil {
			return nil, e
		}
		k := String(n["key"])
		if seen[k] {
			return nil, ErrInvalid
		}
		seen[k] = true
		entities = append(entities, n)
	}
	name, e := Text(d["name"], 500, false)
	if e != nil {
		return nil, e
	}
	subtree, ok := d["include_children"].(bool)
	if !ok {
		return nil, ErrInvalid
	}
	sort.Slice(entities, func(i, j int) bool { return String(entities[i]["key"]) < String(entities[j]["key"]) })
	fp := Hash([]byte(Canonical(Object{"name": name, "subtree": subtree, "entities": entities})))
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		b, e := base(ctx, tx, id)
		if e != nil {
			return nil, e
		}
		if b["archived"] != int64(0) {
			return nil, ErrConflict
		}
		want, ok := Int(d["expected_revision"])
		revision := b["revision"].(int64)
		if !ok || want != revision {
			if ok && fp == b["snapshot_hash"] && want == revision-1 {
				b["unchanged"] = true
				return b, nil
			}
			return nil, ErrConflict
		}
		changed := fp != b["snapshot_hash"]
		ids := []string{}
		affected := map[string]bool{id: true}
		for _, entity := range entities {
			eid := Identity(String(b["connection"]), String(b["library"]), String(entity["key"]))
			ids = append(ids, eid)
			payload := Canonical(entity)
			fingerprint := Hash([]byte(payload))
			var oldFP string
			var rev int64
			e := tx.QueryRowContext(ctx, "SELECT fingerprint,revision FROM entities WHERE id=?", eid).Scan(&oldFP, &rev)
			if e != nil && e != sql.ErrNoRows {
				return nil, e
			}
			if e == nil && oldFP == fingerprint {
				continue
			}
			rev++
			_, e = tx.ExecContext(ctx, `INSERT INTO entities VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,kind=excluded.kind,fingerprint=excluded.fingerprint,revision=excluded.revision`, eid, b["connection"], b["library"], entity["key"], entity["kind"], payload, fingerprint, rev)
			if e != nil {
				return nil, e
			}
			if _, e = tx.ExecContext(ctx, "INSERT INTO entity_revisions VALUES(?,?,?)", eid, rev, payload); e != nil {
				return nil, e
			}
			refs, e := rows(ctx, tx, "SELECT kb_id FROM memberships WHERE entity_id=?", eid)
			if e != nil {
				return nil, e
			}
			for _, ref := range refs {
				affected[String(ref["kb_id"])] = true
			}
			changed = true
		}
		if !changed {
			b["unchanged"] = true
			return b, nil
		}
		if _, e = tx.ExecContext(ctx, "DELETE FROM memberships WHERE kb_id=?", id); e != nil {
			return nil, e
		}
		for _, eid := range ids {
			if _, e = tx.ExecContext(ctx, "INSERT INTO memberships VALUES(?,?)", id, eid); e != nil {
				return nil, e
			}
		}
		for kb := range affected {
			if _, e = tx.ExecContext(ctx, "UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", Now(), kb); e != nil {
				return nil, e
			}
			if kb != id {
				if _, e = tx.ExecContext(ctx, "UPDATE knowledge_bases SET snapshot_hash=NULL WHERE id=?", kb); e != nil {
					return nil, e
				}
			}
		}
		if _, e = tx.ExecContext(ctx, "UPDATE knowledge_bases SET name=?,subtree=?,snapshot_hash=? WHERE id=?", name, subtree, fp, id); e != nil {
			return nil, e
		}
		b, e = base(ctx, tx, id)
		if e != nil {
			return nil, e
		}
		b["unchanged"] = false
		return b, nil
	})
}
