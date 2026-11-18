package store

import (
	"archive/zip"
	"context"
	"io"
	"os"
	"path/filepath"
)

func (s *Store) Export(ctx context.Context, destination string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	temp, err := os.MkdirTemp(filepath.Dir(destination), ".research-backup-")
	if err != nil {
		return err
	}
	defer os.RemoveAll(temp)
	dbPath := filepath.Join(temp, "knowledge.sqlite3")
	if _, err = s.DB.ExecContext(ctx, "VACUUM INTO ?", dbPath); err != nil {
		return err
	}
	db, err := OpenDB(dbPath)
	if err != nil {
		return err
	}
	defer db.Close()
	objects, err := rows(ctx, db, "SELECT hash,report FROM objects")
	if err != nil {
		return err
	}
	for _, o := range objects {
		report, err := Decode(String(o["report"]))
		if err != nil {
			return err
		}
		delete(report, "mineru")
		if _, err = db.ExecContext(ctx, "UPDATE objects SET report=? WHERE hash=?", Canonical(report), o["hash"]); err != nil {
			return err
		}
	}
	var count int
	if err = db.QueryRowContext(ctx, "SELECT COUNT(*) FROM sqlite_master WHERE name='parsing_jobs'").Scan(&count); err != nil {
		return err
	}
	if count > 0 {
		if _, err = db.ExecContext(ctx, "DELETE FROM parsing_jobs"); err != nil {
			return err
		}
	}
	notes, err := rows(ctx, db, "SELECT id,revision,payload FROM local_notes UNION ALL SELECT id,revision,payload FROM entities WHERE kind IN ('note','annotation')")
	if err != nil {
		return err
	}
	if err = db.Close(); err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(temp, "backup.zip"), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	archive := zip.NewWriter(f)
	defer f.Close()
	defer archive.Close()
	hashes := Object{}
	add := func(name string, content []byte) error {
		w, err := archive.Create(name)
		if err != nil {
			return err
		}
		if _, err = w.Write(content); err != nil {
			return err
		}
		hashes[name] = Hash(content)
		return nil
	}
	database, err := os.ReadFile(dbPath)
	if err != nil {
		return err
	}
	if err = add("knowledge.sqlite3", database); err != nil {
		return err
	}
	for _, o := range objects {
		sha := String(o["hash"])
		if !validHash.MatchString(sha) {
			return ErrInvalid
		}
		content, err := os.ReadFile(filepath.Join(s.Home, "objects", sha+".pdf"))
		if err != nil {
			return err
		}
		if Hash(content) != sha {
			return ErrInvalid
		}
		if err = add("objects/"+sha+".pdf", content); err != nil {
			return err
		}
	}
	for _, note := range notes {
		p, err := Decode(String(note["payload"]))
		if err != nil {
			return err
		}
		id := String(note["id"])
		if !validKey.MatchString(id) {
			return ErrInvalid
		}
		name := "notes/" + id + "-" + Canonical(note["revision"]) + ".md"
		content := "# " + String(p["title"]) + "\n\n" + String(p["body"]) + "\n\n" + String(p["comment"]) + "\n"
		if err = add(name, []byte(content)); err != nil {
			return err
		}
	}
	w, err := archive.Create("manifest.json")
	if err != nil {
		return err
	}
	if _, err = io.WriteString(w, Canonical(Object{"format": "research-zotero-backup", "version": 1, "created_at": Now(), "files": hashes})); err != nil {
		return err
	}
	if err = archive.Close(); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return os.Rename(f.Name(), destination)
}
