package store

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"sync"

	_ "modernc.org/sqlite"
)

type Store struct {
	DB   *sql.DB
	Home string
	mu   sync.Mutex
}
type queryer interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

const schema = `
CREATE TABLE IF NOT EXISTS kb_schema(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO kb_schema VALUES(1);
CREATE TABLE IF NOT EXISTS knowledge_bases(id TEXT PRIMARY KEY,connection TEXT NOT NULL,library TEXT NOT NULL,collection TEXT NOT NULL,name TEXT NOT NULL,subtree INTEGER NOT NULL,revision INTEGER NOT NULL,archived INTEGER NOT NULL DEFAULT 0,snapshot_hash TEXT,updated_at TEXT NOT NULL,UNIQUE(connection,library,collection));
CREATE TABLE IF NOT EXISTS objects(hash TEXT PRIMARY KEY,pages TEXT NOT NULL,report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY,connection TEXT NOT NULL,library TEXT NOT NULL,item_key TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,revision INTEGER NOT NULL,UNIQUE(connection,library,item_key));
CREATE TABLE IF NOT EXISTS entity_revisions(id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision));
CREATE TABLE IF NOT EXISTS memberships(kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),entity_id TEXT NOT NULL REFERENCES entities(id),PRIMARY KEY(kb_id,entity_id));
CREATE TABLE IF NOT EXISTS local_notes(id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),payload TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS note_revisions(id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,revision));`

func OpenDB(path string) (*sql.DB, error) {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return nil, err
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if _, err = db.Exec(`PRAGMA foreign_keys=ON; PRAGMA busy_timeout=15000;`); err != nil {
		db.Close()
		return nil, err
	}
	return db, nil
}
func Open(root string) (*Store, error) {
	home := filepath.Join(root, ".data", "zotero")
	if err := os.MkdirAll(filepath.Join(home, "objects"), 0700); err != nil {
		return nil, err
	}
	db, err := OpenDB(filepath.Join(home, "knowledge.sqlite3"))
	if err != nil {
		return nil, err
	}
	if _, err = db.Exec(schema); err != nil {
		db.Close()
		return nil, err
	}
	var version int
	if err = db.QueryRow("SELECT MAX(version) FROM kb_schema").Scan(&version); err != nil || version != 1 {
		db.Close()
		return nil, ErrInvalid
	}
	return &Store{DB: db, Home: home}, nil
}
func rows(ctx context.Context, q queryer, sqlText string, args ...any) ([]Object, error) {
	r, err := q.QueryContext(ctx, sqlText, args...)
	if err != nil {
		return nil, err
	}
	defer r.Close()
	cols, err := r.Columns()
	if err != nil {
		return nil, err
	}
	out := []Object{}
	for r.Next() {
		v := make([]any, len(cols))
		ptr := make([]any, len(cols))
		for i := range v {
			ptr[i] = &v[i]
		}
		if err := r.Scan(ptr...); err != nil {
			return nil, err
		}
		m := Object{}
		for i, c := range cols {
			m[c] = v[i]
		}
		out = append(out, m)
	}
	return out, r.Err()
}
func base(ctx context.Context, q queryer, id string) (Object, error) {
	r, e := rows(ctx, q, "SELECT * FROM knowledge_bases WHERE id=?", id)
	if e != nil {
		return nil, e
	}
	if len(r) == 0 {
		return nil, sql.ErrNoRows
	}
	return r[0], nil
}
func (s *Store) Base(ctx context.Context, id string) (Object, error) { return base(ctx, s.DB, id) }
func (s *Store) Bases(ctx context.Context) ([]Object, error) {
	return rows(ctx, s.DB, `SELECT k.*,COUNT(m.entity_id) AS member_count FROM knowledge_bases k LEFT JOIN memberships m ON m.kb_id=k.id GROUP BY k.id ORDER BY k.name,k.id`)
}
func (s *Store) write(ctx context.Context, f func(*sql.Tx) (Object, error)) (Object, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	tx, e := s.DB.BeginTx(ctx, nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	// Acquire the write lock before reading a revision (SQLite BEGIN IMMEDIATE semantics).
	if _, e = tx.ExecContext(ctx, "UPDATE kb_schema SET version=version WHERE 0"); e != nil {
		return nil, e
	}
	v, e := f(tx)
	if e != nil {
		return nil, e
	}
	if e = tx.Commit(); e != nil {
		return nil, e
	}
	return v, nil
}
func (s *Store) Create(ctx context.Context, d Object) (Object, error) {
	c, e := Key(d["connection"])
	if e != nil {
		return nil, e
	}
	l, e := Key(d["library"])
	if e != nil {
		return nil, e
	}
	col, e := Key(d["collection"])
	if e != nil {
		return nil, e
	}
	n, e := Text(d["name"], 500, false)
	if e != nil {
		return nil, e
	}
	subtree, ok := Default(d, "include_children", true).(bool)
	if !ok {
		return nil, ErrInvalid
	}
	id := Identity(c, l, col)
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		_, e := tx.ExecContext(ctx, "INSERT OR IGNORE INTO knowledge_bases VALUES(?,?,?,?,?,?,0,0,NULL,?)", id, c, l, col, n, subtree, Now())
		if e != nil {
			return nil, e
		}
		return base(ctx, tx, id)
	})
}
func (s *Store) Archive(ctx context.Context, id string, d Object) (Object, error) {
	a, ok := d["archived"].(bool)
	if !ok {
		return nil, ErrInvalid
	}
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		b, e := base(ctx, tx, id)
		if e != nil {
			return nil, e
		}
		want, ok := Int(d["expected_revision"])
		if !ok || want != b["revision"] {
			return nil, ErrConflict
		}
		_, e = tx.ExecContext(ctx, "UPDATE knowledge_bases SET archived=?,revision=revision+1,updated_at=? WHERE id=?", a, Now(), id)
		if e != nil {
			return nil, e
		}
		return base(ctx, tx, id)
	})
}
