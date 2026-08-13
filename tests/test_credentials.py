import unittest

from agent_army.credentials import SessionCredentialBroker


class SessionCredentialBrokerTests(unittest.TestCase):
    def test_reads_each_secret_once_per_session(self) -> None:
        requested = []

        def loader(secret_ref: str) -> str:
            requested.append(secret_ref)
            return f"value:{secret_ref}"

        with SessionCredentialBroker(loader) as broker:
            self.assertEqual(broker.get_secret("doku-key"), "value:doku-key")
            self.assertEqual(broker.get_secret("doku-key"), "value:doku-key")

        self.assertEqual(requested, ["doku-key"])

    def test_closed_broker_refuses_secret_access(self) -> None:
        broker = SessionCredentialBroker(lambda _: "secret")
        broker.close()

        with self.assertRaises(RuntimeError):
            broker.get_secret("doku-key")
