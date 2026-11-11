"""Loader tests against an in-memory fake FHIR server. No network.

    python3 -m unittest discover -s tests -v
"""

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import garmin_fhir as gf  # noqa: E402

SYS = "https://example.org/fhir/identifier/observation"


class FakeServer(gf.FhirClient):
    """Implements just enough of a FHIR server: conditional-update transactions,
    conditional deletes, Binary create/delete and terminology search."""

    def __init__(self):
        super().__init__("http://fake", "id", "secret", sleep=lambda s: None)
        self.store: dict[str, dict] = {}          # conditional url -> resource
        self.binaries: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []
        self.quota_failures = 0                   # transactions to refuse with 429 entries
        self.fail_transaction = False
        self.next_id = 0

    def request(self, method, path, body=None, *, raw=None, content_type="application/fhir+json", ok=(200, 201)):
        self.calls.append((method, path.split("?")[0]))
        if path.startswith("/fhir/R4/CodeSystem?") or path.startswith("/fhir/R4/ValueSet?"):
            return 200, {"resourceType": "Bundle", "entry": []}
        if method in ("POST", "PUT") and path.startswith(("/fhir/R4/CodeSystem", "/fhir/R4/ValueSet")):
            return 201, body
        if method == "POST" and path == "/fhir/R4/Binary":
            self.next_id += 1
            bid = f"bin{self.next_id}"
            self.binaries[bid] = raw
            return 201, {"resourceType": "Binary", "id": bid}
        if method == "DELETE" and path.startswith("/fhir/R4/Binary/"):
            self.binaries.pop(path.rsplit("/", 1)[1], None)
            return 200, None
        if method == "DELETE":
            self.store.pop(path[len("/fhir/R4/"):], None)
            return 200, None
        if method == "POST" and path == "/fhir/R4":
            if self.fail_transaction:
                raise gf.LoadError("POST /fhir/R4: HTTP 400: bad")
            if self.quota_failures:
                self.quota_failures -= 1
                return 200, {"entry": [{"response": {"status": "429"}} for _ in body["entry"]]}
            statuses = []
            for entry in body["entry"]:
                url = entry["request"]["url"]
                statuses.append("200 OK" if url in self.store else "201 Created")
                self.store[url] = entry["resource"]
            return 200, {"entry": [{"response": {"status": s}} for s in statuses]}
        raise AssertionError(f"unexpected {method} {path}")


def obs(value: str) -> dict:
    return {"resourceType": "Observation", "identifier": [{"system": SYS, "value": value}], "status": "final",
            "code": {"text": value}}


class LoaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "fhir").mkdir()
        self.server = FakeServer()
        self.sleeps: list[float] = []

    def loader(self, project="p1"):
        return gf.Loader(self.server, self.root, project, sleep=self.sleeps.append)

    def ndjson(self, resources) -> Path:
        path = self.root / "fhir" / "day.ndjson"
        path.write_text("".join(json.dumps(r) + "\n" for r in resources))
        return path

    def test_upsert_then_stale_resources_are_deleted(self):
        ld = self.loader()
        first = ld.load_batch("d", self.ndjson([obs("a"), obs("b")]), {})
        self.assertEqual((first["created"], first["updated"]), (2, 0))
        second = ld.load_batch("d", self.ndjson([obs("a")]), first)
        self.assertEqual((second["created"], second["updated"]), (0, 1))
        self.assertEqual([k.split("|")[-1] for k in self.server.store], ["a"])

    def test_quota_exhaustion_waits_and_retries(self):
        self.server.quota_failures = 2
        result = self.loader().load_with_quota("d", self.ndjson([obs("a")]), {})
        self.assertEqual(result["created"], 1)
        self.assertEqual(self.sleeps, [gf.QUOTA_WINDOW_S, gf.QUOTA_WINDOW_S])

    def test_quota_retries_are_bounded(self):
        self.server.quota_failures = gf.QUOTA_RETRIES
        with self.assertRaises(gf.RateLimited):
            self.loader().load_with_quota("d", self.ndjson([obs("a")]), {})
        self.assertEqual(len(self.sleeps), gf.QUOTA_RETRIES - 1)

    def doc(self, data: bytes) -> dict:
        rel = "days/2026/2026-01-02/activities/1/original.zip"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"resourceType": "DocumentReference", "status": "current",
                "identifier": [{"system": "https://example.org/fhir/identifier/document", "value": "activity/1/original"}],
                "content": [{"attachment": {
                    "extension": [{"url": "https://example.org/fhir/StructureDefinition/archive-path", "valueString": rel}],
                    "contentType": "application/zip",
                    "hash": base64.b64encode(hashlib.sha1(data).digest()).decode()}}]}

    def test_binary_uploaded_once_reused_and_replaced(self):
        ld = self.loader()
        first = ld.load_batch("d", self.ndjson([self.doc(b"v1")]), {})
        self.assertEqual(list(self.server.binaries.values()), [b"v1"])
        stored = next(iter(self.server.store.values()))
        self.assertEqual(stored["content"][0]["attachment"]["url"], "Binary/bin1")

        again = ld.load_batch("d", self.ndjson([self.doc(b"v1")]), first)
        self.assertEqual(self.server.calls.count(("POST", "/fhir/R4/Binary")), 1)

        ld.load_batch("d", self.ndjson([self.doc(b"v2")]), again)
        self.assertEqual(self.server.binaries, {"bin2": b"v2"})      # old one deleted

    def test_failed_transaction_removes_new_binary(self):
        self.server.fail_transaction = True
        with self.assertRaises(gf.LoadError):
            self.loader().load_batch("d", self.ndjson([self.doc(b"v1")]), {})
        self.assertEqual(self.server.binaries, {})

    def test_changed_archive_file_is_refused(self):
        doc = self.doc(b"v1")
        (self.root / doc["content"][0]["attachment"]["extension"][0]["valueString"]).write_bytes(b"tampered")
        with self.assertRaises(gf.LoadError):
            self.loader().load_batch("d", self.ndjson([doc]), {})

    def test_new_project_means_full_reload(self):
        ld = self.loader("p1")
        ld.manifest["days"]["2026-01-02"] = {"status": "ok", "sha256": "x"}
        ld.save()
        self.assertEqual(self.loader("p1").manifest["days"]["2026-01-02"]["sha256"], "x")
        self.assertEqual(self.loader("p2").manifest["days"], {})

    def test_conditional_url_encoding(self):
        key = gf.ident_key(obs("2026-01-02/steps"))
        self.assertEqual(gf.conditional_url(key), f"Observation?identifier={SYS}|2026-01-02/steps")


if __name__ == "__main__":
    unittest.main()
