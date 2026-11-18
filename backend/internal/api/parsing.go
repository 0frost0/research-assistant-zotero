package api

import (
	"context"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

func (s *Server) parse(ctx context.Context, connection string, ids []string, config Object) (Object, error) {
	snapshot, err := s.Store.Snapshot(ctx, ids)
	if err != nil {
		return nil, err
	}
	mineru, _ := config["mineru"].(map[string]any)
	seen := map[string]bool{}
	jobs := []Object{}
	for _, entity := range snapshot["entities"].([]Object) {
		data := entity["data"].(map[string]any)
		if data["kind"] != "document" {
			continue
		}
		sha := store.String(data["hash"])
		if seen[sha] {
			continue
		}
		seen[sha] = true
		job, err := s.Store.SubmitParsing(ctx, sha, mineru)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, job)
		if job["status"] != "queued" || job["existing"] == true {
			continue
		}
		id := store.String(job["id"])
		profile := store.String(job["profile"])
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			select {
			case s.parseGate <- struct{}{}:
				defer func() { <-s.parseGate }()
			case <-s.ctx.Done():
				return
			}
			s.Store.ParsingState(s.ctx, id, "running", nil)
			seconds, ok := store.Int(mineru["timeout"])
			if !ok {
				seconds = 1800
			}
			runCtx, cancel := context.WithTimeout(s.ctx, time.Duration(seconds+30)*time.Second)
			defer cancel()
			result, err := s.Worker.Call(runCtx, connection, "parse", Object{"payload": Object{"hash": sha, "config": mineru, "profile": profile}})
			if err == nil {
				err = s.Store.SaveParsed(runCtx, sha, result)
			}
			state := "succeeded"
			var message any
			if err != nil {
				state = "failed"
				message = "解析失败，请检查 MinerU 服务；原文保留。"
				if s.ctx.Err() != nil {
					state = "interrupted"
				}
			}
			finish, done := context.WithTimeout(context.Background(), 5*time.Second)
			defer done()
			s.Store.ParsingState(finish, id, state, message)
		}()
	}
	return Object{"jobs": jobs}, nil
}
