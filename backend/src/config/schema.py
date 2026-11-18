import logging
from sqlalchemy import inspect,text
# Small local app: additive columns are applied at startup so existing demo volumes keep working.
def ensure_schema(engine):
    insp=inspect(engine)
    if 'runs' not in insp.get_table_names(): return
    columns={c['name']:c for c in insp.get_columns('runs')}
    dialect=engine.dialect.name
    statements=[]
    if 'purge_task_id' not in columns: statements.append('ALTER TABLE runs ADD COLUMN purge_task_id INTEGER NULL')
    if 'purged_at' not in columns: statements.append('ALTER TABLE runs ADD COLUMN purged_at VARCHAR(40) NULL')
    if dialect=='mysql':
        # A purged run keeps only a value-free tombstone: snapshot/checksum become nullable/empty.
        if columns.get('snapshot',{}).get('nullable') is False:
            statements.append('ALTER TABLE runs MODIFY snapshot JSON NULL')
        if columns.get('checksum',{}).get('nullable') is False:
            statements.append("ALTER TABLE runs MODIFY checksum VARCHAR(64) NULL DEFAULT ''")
    if not statements: return
    with engine.begin() as conn:
        for ddl in statements:
            try: conn.execute(text(ddl))
            except Exception: logging.exception('schema step skipped: %s',ddl)
