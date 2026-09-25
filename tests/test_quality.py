"""Synthetic quality gates: the fixtures do not establish ASR accuracy."""
import copy
import unittest

from audio_transcribe.quality import assess_quality
from audio_transcribe.timeline import audit_timeline
from audio_transcribe.evaluation import timestamp, review_flags
from audio_transcribe.config import resolve_config


def segment(text, index=0, source="fixture"):
    return {"source_id": source, "start_seconds": index * 2, "end_seconds": index * 2 + 2, "text": text}


class QualityTests(unittest.TestCase):
    def test_context_setting_is_explicit_and_strictly_validated(self):
        for invalid in (True, -1, 0.5, 16385, "0"):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                resolve_config("/synthetic/unused", overrides={"asr": {"max_context": invalid}})
        self.assertEqual(resolve_config("/synthetic/unused", overrides={"asr": {"max_context": 224}})["asr"]["max_context"], 224)

    def test_glossary_ignored_by_no_context_is_visible_without_leaking_terms(self):
        quality = assess_quality([segment("A synthetic sentence.")], asr={"max_context": 0}, glossary={"terms": ["SyntheticTerm"]})
        self.assertIn("glossary_context_disabled", quality["reasons"])
        self.assertIn("Glossary prompting is disabled", quality["findings"][0]["message"])
        self.assertNotIn("SyntheticTerm", repr(quality))
        enabled = assess_quality([segment("A synthetic sentence.")], asr={"max_context": 224}, glossary={"terms": ["SyntheticTerm"]})
        self.assertNotIn("glossary_context_disabled", enabled["reasons"])

    def test_raw_negative_display_is_signed_and_source_boundary_review_isolated(self):
        self.assertEqual(timestamp(-0.02), "-00:00:00.020")
        self.assertEqual(timestamp(1.25, srt=True), "00:00:01,250")
        self.assertFalse(review_flags([segment("A fixture."), segment("A fixture.", source="other")]))
        self.assertEqual(review_flags([segment("A fixture."), segment("A fixture.", 1)])[0]["reasons"], ["identical_adjacent_segment"])

    def test_periodic_multiple_sentences_across_different_line_boundaries(self):
        phrase = "The first synthetic proposition holds. Another invented result follows. "
        words = (phrase * 7).split()
        segments = [segment(" ".join(words[i:i + 7]), index) for index, i in enumerate(range(0, len(words), 7))]
        before = copy.deepcopy(segments)
        quality = assess_quality(segments)
        self.assertEqual(quality["status"], "review_required")
        self.assertIn("repeated_phrase_loop", quality["reasons"])
        self.assertFalse(quality["accuracy_verified"])
        self.assertIsNone(quality["timestamp_valid"])
        self.assertEqual(segments, before)
        self.assertNotIn("proposition", repr(quality))

    def test_punctuation_and_case_do_not_hide_repeat(self):
        texts = ["Synthetic words occur in this example.", "SYNTHETIC words occur in this example!",
                 "Synthetic words occur in this example?", "Synthetic words occur in this example."]
        quality = assess_quality([segment(text, index) for index, text in enumerate(texts)])
        self.assertIn("repeated_phrase_loop", quality["reasons"])

    def test_source_boundaries_do_not_manufacture_repeat(self):
        phrase = "These eight synthetic words are only repeated twice. "
        quality = assess_quality([segment(phrase * 2, source="source-a"), segment(phrase * 2, source="source-b")])
        self.assertEqual(quality["status"], "passed_checks")

    def test_short_deliberate_repetition_is_not_a_large_loop(self):
        quality = assess_quality([segment("Yes, yes. We repeat an important definition. We repeat an important definition.")])
        self.assertEqual(quality["status"], "passed_checks")

    def test_clean_result_never_means_accuracy_verified(self):
        quality = assess_quality([segment("First consider the sample mean; next calculate its variance.")])
        self.assertEqual(quality["status"], "passed_checks")
        self.assertFalse(quality["accuracy_verified"])

    def test_empty_output_requires_review_even_with_valid_timeline(self):
        audit = audit_timeline({"transcription": []}, frames=16000, rate=16000)
        quality = assess_quality([], [{"source_id": "fixture", "audit": audit}])
        self.assertEqual(quality["status"], "review_required")
        self.assertIn("empty_transcript", quality["reasons"])
        self.assertTrue(quality["timestamp_valid"])

    def test_invalid_end_is_visible_and_does_not_become_valid(self):
        audit = audit_timeline({"transcription": [{"offsets": {"from": 0, "to": 1200}, "text": "Fixture."}]}, frames=16000, rate=16000)
        quality = assess_quality([segment("Fixture.")], [{"source_id": "fixture", "audit": audit}])
        self.assertFalse(quality["timestamp_valid"])
        self.assertEqual(quality["findings"][0]["reason"], "end_after_eof")
