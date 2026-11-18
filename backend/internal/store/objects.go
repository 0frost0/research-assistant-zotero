package store

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
)

func (s *Store) PutExtracted(ctx context.Context, sha string, content []byte, extracted Object) (Object, error) {
	if !validHash.MatchString(sha) || Hash(content) != sha || len(content) > 25*1024*1024 || len(content) < 5 || string(content[:5]) != "%PDF-" {
		return nil, ErrInvalid
	}
	if ok, err := s.ObjectExists(ctx, sha); err != nil {
		return nil, err
	} else if ok {
		return Object{"hash": sha, "cached": true}, nil
	}
	pages, ok := extracted["pages"].([]any)
	if !ok {
		return nil, ErrInvalid
	}
	report, ok := extracted["report"].(map[string]any)
	if !ok {
		return nil, ErrInvalid
	}
	for _, raw := range pages {
		page, ok := raw.(map[string]any)
		if !ok {
			return nil, ErrInvalid
		}
		if _, ok = page["text"].(string); !ok {
			return nil, ErrInvalid
		}
		if n, ok := Int(page["page"]); !ok || n < 1 {
			return nil, ErrInvalid
		}
	}
	for _, k := range []string{"total_pages", "native_pages", "low_text_pages", "empty_pages"} {
		if _, ok := report[k]; !ok {
			return nil, ErrInvalid
		}
	}
	f, err := os.CreateTemp(filepath.Join(s.Home, "objects"), "*.part")
	if err != nil {
		return nil, err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(content); err != nil {
		f.Close()
		return nil, err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return nil, err
	}
	if err = f.Close(); err != nil {
		return nil, err
	}
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		var existing int
		if err := tx.QueryRowContext(ctx, "SELECT COUNT(*) FROM objects WHERE hash=?", sha).Scan(&existing); err != nil {
			return nil, err
		}
		path := filepath.Join(s.Home, "objects", sha+".pdf")
		if err := os.Rename(f.Name(), path); err != nil {
			return nil, err
		}
		// Preserve immutable extraction and note provenance if repairing lost bytes.
		if existing == 0 {
			if _, err := tx.ExecContext(ctx, "INSERT INTO objects VALUES(?,?,?)", sha, Canonical(pages), Canonical(report)); err != nil {
				return nil, err
			}
		}
		return Object{"hash": sha, "cached": false, "report": report}, nil
	})
}
