import unittest

import db


class Response(object):
    def __init__(self, body, status_code=200):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http %s" % self.status_code)

    def json(self):
        return self._body


class Store(db.Database):
    def __init__(self, removed=None, user=None):
        self.base = "https://example.test/rest/v1"
        self.removed = list(removed or [])
        self.user = user
        self.calls = []

    def _send(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        if url.endswith("/sma_removed"):
            return Response(self.removed)
        return Response([self.user] if self.user else [])


class RemovedCacheTest(unittest.TestCase):
    def setUp(self):
        db._REMOVED_UNTIL = 0.0
        db._REMOVED_NAMES = frozenset()
        db._USER_CACHE.clear()

    def test_the_removed_list_is_read_once(self):
        store = Store([{"username": "Gone"}])
        self.assertTrue(store.is_removed("gone"))
        self.assertTrue(store.is_removed("GONE"))
        self.assertFalse(store.is_removed("kept"))
        self.assertEqual(len(store.calls), 1)

    def test_an_account_row_is_reused(self):
        store = Store(user={"username": "kept", "name": "Kept"})
        self.assertEqual(store.get_user("kept")["name"], "Kept")
        self.assertEqual(store.get_user("KEPT")["username"], "kept")
        self.assertEqual(len(store.calls), 1)

    def test_a_missing_account_is_asked_again(self):
        store = Store()
        self.assertIsNone(store.get_user("new"))
        store.user = {"username": "new", "name": "New"}
        self.assertEqual(store.get_user("new")["name"], "New")
        self.assertEqual(len(store.calls), 2)

    def test_forgetting_an_account_reads_it_again(self):
        store = Store(user={"username": "kept", "name": "Old"})
        self.assertEqual(store.get_user("kept")["name"], "Old")
        db._forget_user("kept")
        store.user = {"username": "kept", "name": "New"}
        self.assertEqual(store.get_user("kept")["name"], "New")
        self.assertEqual(len(store.calls), 2)


if __name__ == "__main__":
    unittest.main()
