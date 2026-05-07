import sqlite3
from db_interface import CourseDatabaseInterface


class SQLiteClient(CourseDatabaseInterface):
    """
    SQLite implementation of CourseDatabaseInterface.

    Uses a single persistent connection (WAL mode) rather than reconnecting
    on every call.  All operations should be called from the same thread;
    worker threads should only call the LLM and hand results back to the
    main thread for DB writes.
    """

    def __init__(self, db_path: str = "course_pipeline.db"):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL mode gives better read concurrency and crash recovery
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.setup_database()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ── schema ────────────────────────────────────────────────────────────────

    def setup_database(self) -> None:
        """Create the schema and indexes.  Safe to call repeatedly."""
        self._conn.execute('''
            CREATE TABLE IF NOT EXISTS course_items (
                id                   TEXT PRIMARY KEY,
                course_id            TEXT NOT NULL,
                item_type            TEXT NOT NULL,
                title                TEXT NOT NULL,
                raw_content          TEXT,
                recommendations      TEXT,
                evaluation           TEXT,
                ai_enhanced_markdown TEXT,
                status               TEXT DEFAULT \'PENDING\',
                created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at           DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Migrate: add evaluation column if the DB was created before this version
        try:
            self._conn.execute("ALTER TABLE course_items ADD COLUMN evaluation TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists — expected on re-runs

        # Composite index for the most common query pattern
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_course_status "
            "ON course_items (course_id, status)"
        )
        self._conn.commit()

    # ── writes ────────────────────────────────────────────────────────────────

    def insert_raw_item(
        self,
        item_id:     str,
        course_id:   str,
        item_type:   str,
        title:       str,
        raw_content: str,
        status:      str = "PENDING",
    ) -> None:
        """Insert a new item.  Silently ignored if item_id already exists."""
        self._conn.execute(
            "INSERT OR IGNORE INTO course_items "
            "(id, course_id, item_type, title, raw_content, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (item_id, course_id, item_type, title, raw_content, status),
        )
        self._conn.commit()

    def update_item_evaluation(
        self, item_id: str, course_id: str, evaluation_json: str
    ) -> None:
        """Store the LLM evaluation JSON string for an item."""
        self._conn.execute(
            "UPDATE course_items "
            "SET evaluation = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND course_id = ?",
            (evaluation_json, item_id, course_id),
        )
        self._conn.commit()

    def update_enhanced_item(
        self, item_id: str, course_id: str, ai_markdown: str
    ) -> None:
        """Mark an item COMPLETED with its AI-enhanced HTML/markdown content."""
        self._conn.execute(
            "UPDATE course_items "
            "SET ai_enhanced_markdown = ?, status = \'COMPLETED\', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND course_id = ?",
            (ai_markdown, item_id, course_id),
        )
        self._conn.commit()

    # ── reads ─────────────────────────────────────────────────────────────────

    def get_pending_items(self, course_id: str | None = None) -> list:
        """Return PENDING items, optionally filtered by course."""
        if course_id:
            rows = self._conn.execute(
                "SELECT id, course_id, item_type, title, raw_content, "
                "recommendations, evaluation "
                "FROM course_items "
                "WHERE status = \'PENDING\' AND course_id = ?",
                (course_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, course_id, item_type, title, raw_content, "
                "recommendations, evaluation "
                "FROM course_items WHERE status = \'PENDING\'"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_items_for_evaluation(self, course_id: str) -> list:
        """
        Return items that have not yet been evaluated for this course.
        Excludes the syllabus (it's used for context, not scored).
        """
        rows = self._conn.execute(
            "SELECT id, course_id, item_type, title, raw_content "
            "FROM course_items "
            "WHERE course_id = ? "
            "  AND item_type != \'syllabus\' "
            "  AND (evaluation IS NULL OR evaluation = \'\') ",
            (course_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_item_by_type(self, course_id: str, item_type: str) -> dict | None:
        """Return the first item matching a given type (e.g. \'syllabus\')."""
        row = self._conn.execute(
            "SELECT id, course_id, item_type, title, raw_content "
            "FROM course_items "
            "WHERE course_id = ? AND item_type = ? LIMIT 1",
            (course_id, item_type),
        ).fetchone()
        return dict(row) if row else None

    def get_course_list(self) -> list:
        """Return a list of distinct courses with item counts and timestamps."""
        rows = self._conn.execute(
            "SELECT course_id, "
            "  COUNT(*) as item_count, "
            "  MAX(updated_at) as last_updated, "
            "  MIN(created_at) as first_created "
            "FROM course_items "
            "WHERE item_type != 'syllabus' "
            "GROUP BY course_id "
            "ORDER BY MAX(updated_at) DESC"
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            # Try to get course name from the first item's title context
            name_row = self._conn.execute(
                "SELECT title FROM course_items "
                "WHERE course_id = ? AND item_type = 'syllabus' LIMIT 1",
                (d["course_id"],),
            ).fetchone()
            d["course_name"] = dict(name_row)["title"] if name_row else f"Course {d['course_id']}"
            results.append(d)
        return results

    def get_completed_items(self, course_id: str | None = None) -> list:
        """Return COMPLETED items for HAX site building."""
        if course_id:
            rows = self._conn.execute(
                "SELECT id, course_id, item_type, title, raw_content, "
                "ai_enhanced_markdown, evaluation, created_at, updated_at "
                "FROM course_items "
                "WHERE status = \'COMPLETED\' AND course_id = ?",
                (course_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, course_id, item_type, title, raw_content, "
                "ai_enhanced_markdown, evaluation, created_at, updated_at "
                "FROM course_items WHERE status = \'COMPLETED\'"
            ).fetchall()
        return [dict(r) for r in rows]
