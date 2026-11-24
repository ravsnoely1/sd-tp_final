import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from chat.security import hash_password, verify_password
from chat.storage import DomainError, Store


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(str(Path(self.temp.name) / "test.db"))
        self.store.initialize("auth")
        self.store.initialize("chat")
        self.alice = self.store.register("alice", "password123")
        self.bob = self.store.register("bob", "password456")
        self.eve = self.store.register("eve", "password789")

    def direct(self):
        return self.store.create_room(self.alice, "direct", "", [self.bob])

    def test_password_salt_and_verification(self):
        first, second = hash_password("my-password"), hash_password("my-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("my-password", first))
        self.assertFalse(verify_password("wrong", first))

    def test_credentials_and_revocation(self):
        with self.assertRaises(DomainError) as failure:
            self.store.login("alice", "wrong")
        self.assertEqual(401, failure.exception.status)
        session = self.store.login("ALICE", "password123")
        self.assertEqual(self.alice, self.store.authenticate(session["token"]))
        self.store.logout(session["token"])
        with self.assertRaises(DomainError):
            self.store.authenticate(session["token"])

    def test_expired_session(self):
        session = self.store.login("alice", "password123")
        with self.store.connection(write=True) as db:
            self.store.sql(db, "UPDATE auth_sessions SET expires_at=0")
        with self.assertRaises(DomainError):
            self.store.authenticate(session["token"])

    def test_invalid_and_duplicate_registration(self):
        for username, password in [("a", "password"), ("valid", "123"), (None, "password")]:
            with self.assertRaises(DomainError):
                self.store.register(username, password)
        with self.assertRaises(DomainError) as failure:
            self.store.register("ALICE", "password999")
        self.assertEqual(409, failure.exception.status)

    def test_private_room_deduplicated(self):
        room = self.direct()
        reversed_room = self.store.create_room(self.bob, "direct", "", [self.alice])
        self.assertEqual(room["id"], reversed_room["id"])
        self.assertEqual([], self.store.rooms(self.eve["id"]))

    def test_group_metadata_and_members(self):
        room = self.store.create_room(self.alice, "group", "Trabalho de SD", [self.bob, self.eve])
        self.assertEqual("Trabalho de SD", room["name"])
        self.assertEqual(3, len(room["members"]))
        self.assertGreater(room["created_at"], 0)
        with self.assertRaises(DomainError):
            self.store.create_room(self.alice, "group", "", [self.bob])

    def test_acl_send_and_history(self):
        room = self.direct()
        with self.assertRaises(DomainError):
            self.store.send(self.eve, room["id"], "invasão", "forbidden")
        with self.assertRaises(DomainError):
            self.store.messages(room["id"], self.eve["id"])
        self.assertEqual([], self.store.messages(room["id"], self.alice["id"]))

    def test_persistence_pagination_and_restart(self):
        room = self.direct()
        sent = [self.store.send(self.alice, room["id"], f"Mensagem {i}", f"m-{i}") for i in range(5)]
        reopened = Store(self.store.url)
        page = reopened.messages(room["id"], self.bob["id"], limit=2)
        self.assertEqual(sent[-2:], page)
        previous = reopened.messages(room["id"], self.bob["id"], before=page[0]["id"], limit=2)
        self.assertEqual(sent[1:3], previous)

    def test_idempotency_and_atomic_event(self):
        room = self.direct()
        before = self.store.event_head()
        first = self.store.send(self.alice, room["id"], "Olá", "once")
        duplicate = self.store.send(self.alice, room["id"], "Olá", "once")
        self.assertEqual(first, duplicate)
        self.assertEqual(1, len(self.store.events(before)))
        with self.assertRaises(DomainError):
            self.store.send(self.alice, room["id"], "Outro texto", "once")
        self.assertEqual(1, len(self.store.messages(room["id"], self.bob["id"])))

    def test_concurrent_idempotency(self):
        room = self.direct()
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(lambda _: self.store.send(self.alice, room["id"], "Única", "concurrent"), range(20)))
        self.assertEqual(1, len({m["id"] for m in result}))

    def test_message_validation(self):
        room = self.direct()
        for content in (None, "  ", "a" * 4001):
            with self.assertRaises(DomainError):
                self.store.send(self.alice, room["id"], content, "invalid")


if __name__ == "__main__":
    unittest.main()
