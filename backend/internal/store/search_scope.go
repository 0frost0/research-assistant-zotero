package store

import (
	"context"
	"database/sql"
)

type SearchScope struct {
	Revisions map[string]int64
	Sources   []string
}

// ScopeForSearch pins revisions and source identities together, without reading bodies
// or verifying anchors. Memberships already include any descendants synced by Zotero.
func (s *Store) ScopeForSearch(ctx context.Context, ids []string, scope string, sourceFilter any) (SearchScope, error) {
	tx, err := s.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return SearchScope{}, err
	}
	defer tx.Rollback()
	revisions, err := activeBaseRevisions(ctx, tx, ids)
	if err != nil {
		return SearchScope{}, err
	}
	// The public source filter selects PDFs only, including for scope=both.
	const query = `
		WITH selected AS (SELECT value AS id FROM json_each(?)),
		     params AS (SELECT ? AS scope, ? AS pdf_filter),
		     members AS (
		         SELECT e.id, e.kind, e.revision, e.payload FROM entities e
		         JOIN memberships m ON m.entity_id=e.id
		         WHERE m.kb_id IN (SELECT id FROM selected)
		     )
		SELECT json_extract(e.payload, '$.hash') || '.pdf' AS source_id
		FROM members e, params p
		WHERE e.kind='document' AND p.scope<>'notes'
		  AND (p.pdf_filter='null' OR json_extract(e.payload, '$.hash') || '.pdf'
		       IN (SELECT value FROM json_each(p.pdf_filter)))
		UNION
		SELECT 'notes/' || e.id || '-' || e.revision || '.md'
		FROM members e, params p
		WHERE e.kind<>'document' AND p.scope<>'original' AND json_extract(e.payload, '$.confirmed')=1
		UNION
		SELECT 'notes/' || n.id || '-' || n.revision || '.md'
		FROM local_notes n, params p
		WHERE n.kb_id IN (SELECT id FROM selected) AND p.scope<>'original'
		  AND json_extract(n.payload, '$.confirmed')=1 AND json_extract(n.payload, '$.archived')=0
		ORDER BY source_id`
	entries, err := tx.QueryContext(ctx, query, Canonical(ids), scope, Canonical(sourceFilter))
	if err != nil {
		return SearchScope{}, err
	}
	defer entries.Close()
	sources := []string{}
	for entries.Next() {
		var id string
		if err := entries.Scan(&id); err != nil {
			return SearchScope{}, err
		}
		sources = append(sources, id)
	}
	if err := entries.Err(); err != nil {
		return SearchScope{}, err
	}
	return SearchScope{Revisions: revisions, Sources: sources}, tx.Commit()
}
