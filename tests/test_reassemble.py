import os
import sys
import unittest

root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

import tests.conftest
import __init__ as oracle_plugin
from scripts.sync_skills import chunk_markdown_text


class TestReassemble(unittest.TestCase):
    def setUp(self):
        # 283-word realistic skill document based on usps-address-validator
        self.doc_words = [
            "Validate", "and", "standardize", "US", "postal", "addresses", "using", "the", "USPS", "Web",
            "Tools", "Address", "Information", "APIs.", "This", "skill", "ensures", "addresses", "conform",
            "to", "Postal", "Service", "addressing", "standards", "including", "standard", "abbreviations",
            "ZIP", "Plus", "4", "codes", "and", "delivery", "point", "verification.", "Prerequisites", "require",
            "a", "valid", "USPS", "Web", "Tools", "User", "ID", "configured", "in", "the", "active",
            "environment.", "When", "to", "use:", "the", "user", "provides", "an", "unverified", "or",
            "partially", "formed", "mailing", "address", "needs", "to", "verify", "whether", "a", "location",
            "is", "deliverable", "by", "the", "postal", "carrier", "or", "requires", "canonical", "ZIP",
            "code", "lookup.", "The", "skill", "formats", "XML", "payloads", "submits", "them", "to",
            "the", "USPS", "production", "gateway", "endpoint", "and", "parses", "the", "returned", "AddressValidateResponse",
            "elements.", "Standardization", "cleans", "street", "suffixes", "such", "as", "Avenue", "to", "AVE",
            "Street", "to", "ST", "and", "Boulevard", "to", "BLVD.", "Secondary", "unit", "designators",
            "like", "Suite", "Apartment", "and", "Building", "are", "normalized", "into", "standard", "abbreviations.",
            "If", "an", "address", "cannot", "be", "verified", "or", "has", "multiple", "possible",
            "matches", "the", "API", "returns", "error", "descriptions", "which", "are", "surfaced", "directly",
            "to", "the", "calling", "agent.", "City", "and", "state", "lookup", "is", "performed",
            "against", "the", "CityStateLookup", "API", "endpoint", "when", "only", "the", "5-digit", "ZIP",
            "code", "is", "provided.", "Reverse", "ZIP", "code", "resolution", "resolves", "missing", "ZIP",
            "codes", "from", "city", "and", "state", "inputs.", "Carrier", "route", "information", "and",
            "delivery", "point", "barcodes", "are", "optionally", "extracted", "when", "full", "DPV",
            "confirmation", "data", "is", "available.", "Always", "confirm", "deliverability", "status", "before",
            "dispatching", "physical", "mailings.", "Error", "handling", "distinguishes", "between", "network",
            "transport", "failures", "invalid", "credentials", "and", "undeliverable", "address", "rejections.",
            "Retry", "logic", "applies", "exponential", "backoff", "for", "transient", "HTTP", "500",
            "and", "503", "errors", "from", "the", "USPS", "gateway.", "All", "XML", "entities",
            "are", "escaped", "to", "prevent", "malformed", "request", "structures.", "Returned", "XML",
            "is", "parsed", "with", "strict", "defused", "parsers", "guarding", "against", "entity",
            "expansion", "attacks.", "Output", "formats", "include", "canonical", "single-line", "representations",
            "and", "structured", "JSON", "dictionaries", "containing", "separate", "fields", "for", "address1",
            "address2", "city", "state", "zip5", "and", "zip4.", "Use", "this", "skill",
            "whenever", "address", "integrity", "is", "mission", "critical", "for", "fulfillment", "and",
            "shipping", "workflows."
        ]
        self.original_text = " ".join(self.doc_words)

    def test_golden_fixture_reassembly_fidelity(self):
        """Verify 5 overlapping chunks reassemble to exact original word sequence (0 missing, 0 duplicate)."""
        # Generate 5 overlapping chunks using chunker (max_words=80, overlap=10)
        chunks = chunk_markdown_text(self.original_text, max_words=80, overlap=10)
        self.assertEqual(len(chunks), 5)

        # Prefix chunks with skill ingest headers
        prefixed_chunks = [
            f"[skill_ingest] [skill:usps-address-validator] usps-address-validator (chunk {i+1}/5):\n{c}"
            for i, c in enumerate(chunks)
        ]

        reassembled = oracle_plugin.reassemble_chunks(prefixed_chunks)
        reassembled_words = reassembled.split()

        # Word count must match original exactly
        self.assertEqual(len(reassembled_words), len(self.doc_words))
        # Sequence of words must match identically
        self.assertEqual(reassembled_words, self.doc_words)

    def test_edge_case_chunk_under_10_words(self):
        """Verify reassembly when a chunk has fewer than 10 words."""
        c1 = "[skill_ingest] [skill:test] test (chunk 1/2):\nAlpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet"
        # Overlap last 4 words: Golf Hotel India Juliet, then 3 new words (total 7 words < 10)
        c2 = "[skill_ingest] [skill:test] test (chunk 2/2):\nGolf Hotel India Juliet Kilo Lima Mike"

        reassembled = oracle_plugin.reassemble_chunks([c1, c2])
        expected = "Alpha Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliet Kilo Lima Mike"
        self.assertEqual(reassembled, expected)

    def test_edge_case_disjoint_non_matching_overlap(self):
        """Verify reassembly when two chunks have completely disjoint words (overlap = 0)."""
        c1 = "[skill_ingest] [skill:test] test (chunk 1/2):\nFirst part of disconnected document."
        c2 = "[skill_ingest] [skill:test] test (chunk 2/2):\nSecond section with entirely fresh vocabulary."

        reassembled = oracle_plugin.reassemble_chunks([c1, c2])
        expected = "First part of disconnected document. Second section with entirely fresh vocabulary."
        self.assertEqual(reassembled, expected)

    def test_empty_chunks(self):
        self.assertEqual(oracle_plugin.reassemble_chunks([]), "")
        self.assertEqual(oracle_plugin.reassemble_chunks(["[skill_ingest] [skill:x]:\n"]), "")


if __name__ == "__main__":
    unittest.main()
