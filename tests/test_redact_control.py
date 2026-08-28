"""Positive control for the redaction gate.

A gate that has only ever returned PASS is indistinguishable from a gate that cannot fail, and this
one has twice been trusted while checking nothing: once handed a file rather than a directory, once
with an empty site-term list. Both now refuse, and this asserts they do.

So every class the gate screens for is planted here and must come back named. Two negative controls
sit alongside them, because a gate that fails on everything is no more useful than one that passes
on everything: a clean artifact must pass, and a checksum the document DECLARES must stay exempt.
That exemption is deliberate (publishing the digest of your own measurement code is the opposite of
a leak) and it is the one place where the difference between narrow and absent is easy to get
wrong. Writing this test caught the author asserting the opposite: the first draft planted the word
"digest" beside the blob and read the intended exemption as a miss.

unittest and the standard library only, and the gate is invoked as a subprocess through the same
entry point an operator uses, so what is under test is the shipped command rather than an import of
its internals.
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# One instance of every structural class in redact.PATTERNS. All are RFC 5737 / RFC 7042
# documentation values or obvious placeholders, so this file stays clean under its own gate.
CASES = {
    "IPv4 address": "the host answered on 203.0.113.7 during the run",
    "MAC address": "link layer 02:42:ac:11:00:02 seen on the bridge",
    "GPU UUID": "device GPU-1a2b3c4d-0000-0000-0000-000000000000 reported",
    "email address": "contact ops.person@example-corp.com for the window",
    "JWT": "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0",
    "home directory path": "model cached under /home/operator/.cache/huggingface",
    "Windows user path": "built from C:\\Users\\SomeOperator\\projects\\thing",
    "password-ish assignment": "api_key = s3cr3tvalue123",
    # No declaring word on the line, so the checksum exemption must NOT apply here.
    "long hex run": "value 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928 end",
    "long base64 run": "blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5",
}
SITE_TERM = "Contoso Manufacturing"


class RedactionGateControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gpubench-redact-")

    def tearDown(self):
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def gate(self, target, keep_env=True):
        env = dict(os.environ)
        if not keep_env:
            env.pop("GPUBENCH_DENY_LITERALS", None)
        p = subprocess.run([sys.executable, "-m", "gpubench.longform.redact", str(target)],
                           cwd=ROOT, capture_output=True, universal_newlines=True, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    def artifacts(self, name, body, deny=SITE_TERM):
        d = os.path.join(self.tmp, name)
        os.makedirs(d)
        if deny is not None:
            with io.open(os.path.join(d, "denylist.txt"), "w", encoding="utf-8") as f:
                f.write(deny + "\n")
        with io.open(os.path.join(d, "page.html"), "w", encoding="utf-8") as f:
            f.write(body)
        return d

    def test_every_leak_class_is_caught_by_name(self):
        for kind, text in sorted(CASES.items()):
            with self.subTest(kind=kind):
                d = self.artifacts(kind.replace(" ", "_"), "<p>%s</p>\n" % text)
                rc, out = self.gate(d)
                self.assertEqual(rc, 1, "gate passed a planted %s: %s" % (kind, out[:300]))
                self.assertIn(kind, out,
                              "caught something, but not named as %s: %s" % (kind, out[:300]))

    def test_site_name_is_caught(self):
        """The half that was silently absent when a scratch copy lost its denylist."""
        d = self.artifacts("named", "<p>measured at %s this quarter</p>" % SITE_TERM)
        rc, out = self.gate(d)
        self.assertEqual(rc, 1, out[:300])
        self.assertIn("literal", out, out[:300])

    def test_clean_artifact_passes(self):
        """Negative control: the gate must not be an unconditional failure."""
        d = self.artifacts(
            "clean", "<p>prefill reached 2,187.8 tokens per second at 2,048 counted tokens.</p>")
        rc, out = self.gate(d)
        self.assertEqual(rc, 0, out[:300])

    def test_declared_checksum_stays_exempt(self):
        """Negative control: a digest the document declares is a transparency measure, not a leak."""
        d = self.artifacts(
            "declared", "<p>sha256 9f8e7d6c5b4a39281706f5e4d3c2b1a09f8e7d6c5b4a3928</p>")
        rc, out = self.gate(d)
        self.assertEqual(rc, 0, "the checksum exemption has become too narrow: %s" % out[:300])

    def test_refuses_a_file_rather_than_a_directory(self):
        """This once printed "PASS: 0 files scanned", which reads exactly like a clean result."""
        d = self.artifacts("one", "<p>harmless</p>")
        rc, out = self.gate(os.path.join(d, "page.html"))
        self.assertNotEqual(rc, 0, out[:300])
        self.assertIn("REFUSING", out, out[:300])

    def test_refuses_when_no_site_terms_are_loaded(self):
        """An edition carrying the organisation name in plain text once passed this way."""
        d = self.artifacts("nodeny", "<p>harmless</p>", deny=None)
        rc, out = self.gate(d, keep_env=False)
        self.assertNotEqual(rc, 0, out[:300])
        self.assertIn("REFUSING", out, out[:300])


if __name__ == "__main__":
    unittest.main()
