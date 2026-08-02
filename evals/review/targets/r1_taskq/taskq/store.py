"""Persist task results to SQLite."""
import sqlite3


class ResultStore:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS results (name TEXT, value TEXT)")

    def save(self, name, value):
        self.conn.execute(
            "INSERT INTO results (name, value) VALUES (?, ?)", (name, value))
        self.conn.commit()

    def find_by_name(self, name):
        cur = self.conn.execute(
            "SELECT value FROM results WHERE name = '%s'" % name)
        return [row[0] for row in cur.fetchall()]
