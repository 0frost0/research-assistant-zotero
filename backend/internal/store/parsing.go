package store

import (
	"context"
	"database/sql"
	"os"
	"strings"

	"github.com/google/uuid"
)

func (s *Store) InitParsing(ctx context.Context) error {
	_, err := s.DB.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS parsing_jobs(id TEXT PRIMARY KEY,sha TEXT NOT NULL,profile TEXT NOT NULL,status TEXT NOT NULL,error TEXT,updated_at TEXT NOT NULL)`)
	if err != nil {
		return err
	}
	_, err = s.DB.ExecContext(ctx, "UPDATE parsing_jobs SET status='interrupted',error=?,updated_at=? WHERE status IN ('queued','running')", "应用重启中断了任务；可手动重试。", Now())
	return err
}
func (s *Store) ParsingJobs(ctx context.Context) ([]Object, error) {
	return rows(ctx, s.DB, "SELECT id,sha,status,error,updated_at FROM parsing_jobs ORDER BY updated_at DESC LIMIT 100")
}
func (s *Store) SubmitParsing(ctx context.Context, sha string, config Object) (Object, error) {
	ok, err := s.ObjectExists(ctx, sha)
	if err != nil {
		return nil, err
	}
	if !ok || config["mode"] != "local" && config["mode"] != "remote" {
		return nil, ErrInvalid
	}
	profile := Hash([]byte(Canonical(config)))
	return s.write(ctx, func(tx *sql.Tx) (Object, error) {
		var raw string
		if err := tx.QueryRowContext(ctx, "SELECT report FROM objects WHERE hash=?", sha).Scan(&raw); err != nil {
			return nil, err
		}
		report, err := Decode(raw)
		if err != nil {
			return nil, err
		}
		parsed, _ := report["mineru"].(map[string]any)
		blocks := items(parsed["blocks"])
		cached := parsed["profile"] == profile && len(blocks) > 0
		for _, block := range blocks {
			meta, _ := block["metadata"].(map[string]any)
			if path := String(meta["image_path"]); path != "" {
				if _, err := os.Stat(path); err != nil {
					cached = false
				}
			}
		}
		if cached {
			return Object{"id": nil, "status": "succeeded", "cached": true}, nil
		}
		rs, err := rows(ctx, tx, "SELECT id,status FROM parsing_jobs WHERE sha=? AND profile=? AND status IN ('queued','running')", sha, profile)
		if err != nil {
			return nil, err
		}
		if len(rs) > 0 {
			rs[0]["existing"] = true
			return rs[0], nil
		}
		id := strings.ReplaceAll(uuid.NewString(), "-", "")
		_, err = tx.ExecContext(ctx, "INSERT INTO parsing_jobs VALUES(?,?,?,'queued',NULL,?)", id, sha, profile, Now())
		return Object{"id": id, "status": "queued", "profile": profile}, err
	})
}
func (s *Store) ParsingState(ctx context.Context, id, status string, message any) error {
	_, err := s.DB.ExecContext(ctx, "UPDATE parsing_jobs SET status=?,error=?,updated_at=? WHERE id=?", status, message, Now(), id)
	return err
}
func (s *Store) SaveParsed(ctx context.Context, sha string, parsed Object) error {
	if len(items(parsed["blocks"])) == 0 {
		return ErrInvalid
	}
	_, err := s.write(ctx, func(tx *sql.Tx) (Object, error) {
		var raw string
		if err := tx.QueryRowContext(ctx, "SELECT report FROM objects WHERE hash=?", sha).Scan(&raw); err != nil {
			return nil, err
		}
		report, err := Decode(raw)
		if err != nil {
			return nil, err
		}
		report["mineru"] = parsed
		_, err = tx.ExecContext(ctx, "UPDATE objects SET report=? WHERE hash=?", Canonical(report), sha)
		return nil, err
	})
	return err
}
