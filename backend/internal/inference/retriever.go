package inference

import (
	"context"
	"errors"
	"maps"
	"time"

	"github.com/0frost0/research-assistant-zotero/backend/internal/store"
)

// Retriever pins the Go store's revisions while the worker builds or queries the index.
type Retriever struct {
	*Client
	Store *store.Store
}

func searchBaseIDs(raw any) []string {
	if ids, ok := raw.([]string); ok {
		return ids
	}
	values, _ := raw.([]any)
	ids := make([]string, 0, len(values))
	for _, value := range values {
		ids = append(ids, store.String(value))
	}
	return ids
}

func (r *Retriever) SearchOnce(ctx context.Context, connection string, payload store.Object) (store.Object, error) {
	return r.search(ctx, connection, payload, false)
}

func (r *Retriever) Search(ctx context.Context, connection string, payload store.Object) (store.Object, error) {
	return r.search(ctx, connection, payload, true)
}

func (r *Retriever) search(ctx context.Context, connection string, payload store.Object, wait bool) (store.Object, error) {
	ids := searchBaseIDs(payload["kb_ids"])
	expected, err := r.Store.ActiveBaseRevisions(ctx, ids)
	if err != nil {
		return nil, err
	}
	request := store.Object{"payload": payload, "expected_revisions": expected}
	for {
		result, err := r.Call(ctx, connection, "search", request)
		if err != nil {
			return nil, err
		}
		if result["status"] != "indexing" {
			if err := r.checkRevisions(ctx, ids, expected, result); err != nil {
				return nil, err
			}
			return result, nil
		}
		if !wait {
			return result, nil
		}
		timer := time.NewTimer(500 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
}

func (r *Retriever) checkRevisions(ctx context.Context, ids []string, expected map[string]int64, result store.Object) error {
	revisions, ok := result["revisions"].(map[string]any)
	if !ok || len(revisions) != len(expected) {
		return errors.New("invalid_search_revisions")
	}
	for id, revision := range expected {
		got, ok := store.Int(revisions[id])
		if !ok {
			return errors.New("invalid_search_revisions")
		}
		if got != revision {
			return store.ErrConflict
		}
	}
	current, err := r.Store.ActiveBaseRevisions(ctx, ids)
	if err != nil {
		return err
	}
	if !maps.Equal(current, expected) {
		return store.ErrConflict
	}
	return nil
}
