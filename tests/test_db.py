import sqlite3
import tempfile
import unittest

from solar_monitor.db import Database


class SqliteHygieneTests(unittest.TestCase):
    """The collector writes every poll while web requests read thousands of
    rows on separate connections; neither may wait on the other."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/test.sqlite3"
        self.db = Database(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_the_file_is_in_wal_mode_and_stays_there(self):
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 1, "NORMAL")
        Database(self.path)  # opening the same file again must be harmless
        # The mode lives in the file: a connection that sets nothing finds it.
        plain = sqlite3.connect(self.path)
        try:
            self.assertEqual(plain.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            plain.close()

    def test_a_write_and_a_read_interleave_without_locking(self):
        insert = "INSERT INTO audit_events (occurred_at, action) VALUES (?, ?)"
        # A reader in an open transaction while a writer commits.
        with self.db.connect() as reader:
            reader.execute("BEGIN")
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)
            with self.db.connect() as writer:
                writer.execute(insert, ("t", "first"))
            # The reader neither blocked the commit nor sees past its snapshot.
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 0)
        # A writer with an uncommitted row while a reader reads.
        with self.db.connect() as writer:
            writer.execute(insert, ("t", "second"))
            with self.db.connect() as reader:
                self.assertEqual(reader.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 1)
        self.assertEqual([row["action"] for row in self.db.recent_audit(10)], ["second", "first"])


if __name__ == "__main__":
    unittest.main()
