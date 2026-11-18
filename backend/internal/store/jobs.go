package store

import (
	"context"
	"database/sql"
	"strings"
	"sync"

	"github.com/google/uuid"
)

type Jobs struct {
	DB *sql.DB
	mu sync.Mutex
}

func OpenJobs(path string) (*Jobs, error) {
	db, e := OpenDB(path)
	if e != nil {
		return nil, e
	}
	_, e = db.Exec(`CREATE TABLE IF NOT EXISTS qa_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,started_at TEXT,finished_at TEXT);
CREATE INDEX IF NOT EXISTS qa_jobs_created_at ON qa_jobs(created_at DESC);
CREATE TABLE IF NOT EXISTS qa_feedback(id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,rating TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',question TEXT NOT NULL,scope TEXT NOT NULL,kb_ids TEXT NOT NULL,result_hash TEXT NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS qa_feedback_created_at ON qa_feedback(created_at DESC);`)
	if e != nil {
		db.Close()
		return nil, e
	}
	_, e = db.Exec("UPDATE qa_jobs SET state='interrupted',error=?,updated_at=?,finished_at=? WHERE state IN ('queued','running')", "服务重启中断，未自动重试；已发送的请求可能仍由模型服务计费。", Now(), Now())
	if e != nil {
		db.Close()
		return nil, e
	}
	return &Jobs{DB: db}, nil
}
func job(ctx context.Context, q queryer, id string, include bool) (Object, error) {
	rs, e := rows(ctx, q, "SELECT * FROM qa_jobs WHERE id=?", id)
	if e != nil {
		return nil, e
	}
	if len(rs) == 0 {
		return nil, sql.ErrNoRows
	}
	r := rs[0]
	p, e := Decode(String(r["payload"]))
	if e != nil {
		return nil, e
	}
	v := Object{"job_id": id, "question": String(p["query"]), "scope": Default(p, "scope", "original"), "retry_of": p["_retry_of"], "retry_count": Default(p, "_retry_count", 0), "feedback": nil}
	for _, k := range []string{"kind", "state", "created_at", "updated_at", "started_at", "finished_at", "error"} {
		v[k] = r[k]
	}
	if chars := []rune(String(v["question"])); len(chars) > 500 {
		v["question"] = string(chars[:500])
	}
	if include && r["result"] != nil {
		v["result"], e = Decode(String(r["result"]))
		if e != nil {
			return nil, e
		}
	}
	f, e := rows(ctx, q, "SELECT id AS feedback_id,job_id,rating,note,question,scope,result_hash,created_at FROM qa_feedback WHERE job_id=?", id)
	if e != nil {
		return nil, e
	}
	if len(f) > 0 {
		v["feedback"] = f[0]
	}
	return v, nil
}
func (j *Jobs) Public(ctx context.Context, id string) (Object, error) {
	return job(ctx, j.DB, id, true)
}
func (j *Jobs) Payload(ctx context.Context, id string) (Object, error) {
	var raw string
	e := j.DB.QueryRowContext(ctx, "SELECT payload FROM qa_jobs WHERE id=?", id).Scan(&raw)
	if e != nil {
		return nil, e
	}
	return Decode(raw)
}
func (j *Jobs) List(ctx context.Context) ([]Object, error) {
	tx, e := j.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	rs, e := rows(ctx, tx, "SELECT id FROM qa_jobs ORDER BY created_at DESC LIMIT 20")
	if e != nil {
		return nil, e
	}
	out := []Object{}
	for _, r := range rs {
		v, e := job(ctx, tx, String(r["id"]), false)
		if e != nil {
			return nil, e
		}
		out = append(out, v)
	}
	return out, tx.Commit()
}
func insertJob(ctx context.Context, tx *sql.Tx, p Object) (string, error) {
	raw := Canonical(p)
	if len(raw) > 100000 {
		return "", ErrInvalid
	}
	id := strings.ReplaceAll(uuid.NewString(), "-", "")[:20]
	stamp := Now()
	_, e := tx.ExecContext(ctx, "INSERT INTO qa_jobs(id,kind,payload,state,created_at,updated_at) VALUES(?,'ask',?,'queued',?,?)", id, raw, stamp, stamp)
	if e != nil {
		return "", e
	}
	// Retain active requests even when history reaches its bound.
	stale := `SELECT id FROM qa_jobs WHERE state NOT IN ('queued','running') ORDER BY created_at DESC LIMIT -1 OFFSET 200`
	if _, e = tx.ExecContext(ctx, "DELETE FROM qa_feedback WHERE job_id IN ("+stale+")"); e != nil {
		return "", e
	}
	if _, e = tx.ExecContext(ctx, "DELETE FROM qa_jobs WHERE id IN ("+stale+")"); e != nil {
		return "", e
	}
	return id, nil
}
func (j *Jobs) Create(ctx context.Context, p Object) (Object, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	tx, e := j.DB.BeginTx(ctx, nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	id, e := insertJob(ctx, tx, p)
	if e != nil {
		return nil, e
	}
	v, e := job(ctx, tx, id, true)
	if e != nil {
		return nil, e
	}
	return v, tx.Commit()
}
func (j *Jobs) Retry(ctx context.Context, id string) (Object, error) {
	j.mu.Lock()
	defer j.mu.Unlock()
	tx, e := j.DB.BeginTx(ctx, nil)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback()
	rs, e := rows(ctx, tx, "SELECT kind,state,payload FROM qa_jobs WHERE id=?", id)
	if e != nil {
		return nil, e
	}
	if len(rs) == 0 {
		return nil, sql.ErrNoRows
	}
	r := rs[0]
	if r["kind"] != "ask" || r["state"] != "failed" && r["state"] != "interrupted" {
		return nil, ErrInvalid
	}
	p, e := Decode(String(r["payload"]))
	if e != nil {
		return nil, e
	}
	count, ok := Int(Default(p, "_retry_count", 0))
	if !ok || count >= 3 {
		return nil, ErrInvalid
	}
	clean := Object{}
	for k, v := range p {
		if !strings.HasPrefix(k, "_") {
			clean[k] = v
		}
	}
	clean["_retry_of"] = id
	clean["_retry_count"] = count + 1
	next, e := insertJob(ctx, tx, clean)
	if e != nil {
		return nil, e
	}
	v, e := job(ctx, tx, next, true)
	if e != nil {
		return nil, e
	}
	return v, tx.Commit()
}
func (j *Jobs) MarkRunning(ctx context.Context, id string) error {
	r, e := j.DB.ExecContext(ctx, "UPDATE qa_jobs SET state='running',started_at=?,updated_at=? WHERE id=? AND state='queued'", Now(), Now(), id)
	if e != nil {
		return e
	}
	n, e := r.RowsAffected()
	if e != nil {
		return e
	}
	if n != 1 {
		return ErrInvalid
	}
	return nil
}
func (j *Jobs) Cancel(ctx context.Context, id string) (Object, error) {
	r, e := j.DB.ExecContext(ctx, "UPDATE qa_jobs SET state='cancelled',error=?,updated_at=?,finished_at=? WHERE id=? AND state IN ('queued','running')", "用户主动停止；已发送的模型请求可能仍由服务计费。", Now(), Now(), id)
	if e != nil {
		return nil, e
	}
	v, e := j.Public(ctx, id)
	if e != nil {
		return nil, e
	}
	n, e := r.RowsAffected()
	if e != nil {
		return nil, e
	}
	if n == 0 && v["state"] != "cancelled" {
		return nil, ErrInvalid
	}
	return v, nil
}
func (j *Jobs) Finish(ctx context.Context, id, state string, result Object, message any) (Object, error) {
	if state != "succeeded" && state != "failed" && state != "interrupted" {
		return nil, ErrInvalid
	}
	var raw any
	if result != nil {
		value := Canonical(result)
		if len(value) > 2000000 {
			return nil, ErrInvalid
		}
		raw = value
	}
	_, e := j.DB.ExecContext(ctx, "UPDATE qa_jobs SET state=?,result=?,error=?,updated_at=?,finished_at=? WHERE id=? AND state IN ('queued','running')", state, raw, message, Now(), Now(), id)
	if e != nil {
		return nil, e
	}
	return j.Public(ctx, id)
}
