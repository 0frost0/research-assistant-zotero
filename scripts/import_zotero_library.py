"""Add a local-PDF snapshot without editing Zotero or replacing collection bases.

Input must be a consistent *copy* of Zotero's DB, never its live database.
Reports contain identifiers/counts only, not paper text or credentials.
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.store import KnowledgeStore, digest, now
from research_assistant.knowledge.backup import export_backup

COLLECTION = 'local-readable-pdf-snapshot-v1'
NAME = 'Zotero 本地可读 PDF（快照）'
MAX_BYTES = 25 * 1024 * 1024


def attachment_path(raw, item_key, data_dir, attachment_root=None):
    if raw.startswith('storage:'):
        root, relative = data_dir / 'storage' / item_key, raw[8:]
    elif raw.startswith('attachments:'):
        if attachment_root is None:
            raise ValueError('relative_attachment_root_missing')
        root, relative = attachment_root, raw[12:]
    else:
        path = Path(raw)
        if not path.is_absolute():
            raise ValueError('unsupported_attachment_path')
        return path
    root = root.resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('attachment_path_escape')
    return path


def inventory(snapshot, data_dir, attachment_root=None):
    if snapshot.resolve() == (data_dir / 'zotero.sqlite').resolve():
        raise ValueError('Use a consistent database copy, not the live Zotero DB')
    with closing(sqlite3.connect(snapshot.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid Zotero snapshot')
        rows = con.execute('''SELECT i.key, i.libraryID, a.path, p.key parent_key,
            COALESCE(v.value,'') title, l.type library_type
            FROM itemAttachments a JOIN items i ON i.itemID=a.itemID
            JOIN libraries l ON l.libraryID=i.libraryID
            LEFT JOIN items p ON p.itemID=a.parentItemID
            LEFT JOIN itemData d ON d.itemID=COALESCE(p.itemID,i.itemID)
                AND d.fieldID=(SELECT fieldID FROM fields WHERE fieldName='title')
            LEFT JOIN itemDataValues v ON v.valueID=d.valueID
            WHERE a.contentType='application/pdf' AND NOT EXISTS
                (SELECT 1 FROM deletedItems x WHERE x.itemID=i.itemID OR x.itemID=p.itemID)
            ORDER BY i.key''').fetchall()
    entries, paths = [], {}
    for row in rows:
        entry = {'key': row['key'], 'parent_key': row['parent_key']}
        try:
            if row['library_type'] != 'user':
                raise ValueError('group_library_not_supported')
            path = attachment_path(row['path'], row['key'], data_dir, attachment_root)
            if not path.is_file():
                entry['status'] = 'missing'
            elif path.stat().st_size > MAX_BYTES:
                entry['status'] = 'oversized'
                entry['bytes'] = path.stat().st_size
            else:
                content = path.read_bytes()
                if not content.startswith(b'%PDF-'):
                    raise ValueError('invalid_pdf_header')
                sha = digest(content)
                entry.update(status='ready', hash=sha, bytes=len(content))
                paths[row['key']] = (path, {'key': row['key'], 'kind': 'document',
                    'parent_key': row['parent_key'], 'title': row['title'], 'hash': sha})
        except (OSError, ValueError) as exc:
            entry.update(status='unreadable', error=type(exc).__name__)
        entries.append(entry)
    return entries, paths


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run(args):
    entries, paths = inventory(args.snapshot, args.data_dir, args.attachment_root)
    report = {'created_at': now(), 'mode': 'apply' if args.apply else 'inventory',
              'scope': 'available_user_library_pdfs_under_25MiB_only_not_full_Zotero_library',
              'snapshot_sha256': digest(args.snapshot.read_bytes()),
              'answer_model_calls': 0, 'zotero_writes': False, 'entries': entries}
    if args.apply:
        store = KnowledgeStore(args.root)
        backup = args.report.parent / 'before-import-backup.zip'
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise FileExistsError('Use a fresh report directory; never overwrite the pre-import backup')
        export_backup(store, backup)
        report['backup'] = str(backup.resolve())
        # Reuse matching existing entities verbatim. Do not revise titles/notes
        # shared with older collection bases simply because this is a new scope.
        with store.read() as con:
            existing = {r[0]: json.loads(r[1]) for r in con.execute(
                'SELECT item_key,payload FROM entities WHERE connection=? AND library=?',
                (args.connection, 'user:0'))}
        entities = []
        for entry in entries:
            if entry['status'] != 'ready':
                continue
            path, entity = paths[entry['key']]
            old = existing.get(entry['key'])
            if old and (old.get('kind') != 'document' or old.get('hash') != entry['hash']):
                entry['status'] = 'existing_entity_conflict'
                continue
            try:
                content = path.read_bytes()
                result = store.put_object(entry['hash'], content)
                with store.read() as con:
                    pages, raw = con.execute('SELECT pages,report FROM objects WHERE hash=?',
                                             (entry['hash'],)).fetchone()
                pages, extraction = json.loads(pages), json.loads(raw)
                entry.update(status='imported', cached=result['cached'], pages=len(pages),
                             text_pages=sum(bool(p['text'].strip()) for p in pages),
                             empty_pages=extraction.get('empty_pages', 0),
                             low_text_pages=extraction.get('low_text_pages', 0))
                entities.append(old or entity)
            except Exception as exc:
                entry.update(status='extraction_failed', error=type(exc).__name__)
            report['counts'] = dict(Counter(e['status'] for e in entries))
            save_report(args.report, report)
        # Dedicated positive scope; never silently shrink an earlier import.
        base = store.create({'connection': args.connection, 'library': 'user:0',
                             'collection': COLLECTION, 'name': NAME, 'include_children': False})
        old_members = store.snapshot([base['id']])['entities']
        by_key = {e['data']['key']: e['data'] for e in old_members}
        by_key.update({e['key']: e for e in entities})
        synced = store.sync(base['id'], {'name': NAME, 'include_children': False,
            'expected_revision': base['revision'], 'complete': True, 'entities': list(by_key.values())})
        report.update(base_id=base['id'], revision=synced['revision'],
                      members=len(by_key), unique_pdfs=len({e['hash'] for e in by_key.values()}))
    report['counts'] = dict(Counter(e['status'] for e in entries))
    save_report(args.report, report)
    print(json.dumps({k: v for k, v in report.items() if k != 'entries'}, ensure_ascii=False))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--attachment-root', type=Path)
    parser.add_argument('--connection', required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    run(parser.parse_args())
