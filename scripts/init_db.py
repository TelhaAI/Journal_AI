"""Create tables, install immutability triggers, and sync prompt versions from prompts/."""
from journal_ai.db import init_db, session_scope
from journal_ai.prompts import sync_prompts_from_disk

if __name__ == "__main__":
    init_db()
    with session_scope() as db:
        sync_prompts_from_disk(db)
    print("database initialised; prompt versions synced")
