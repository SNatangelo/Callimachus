"""Contract tests for modular, source-spanned reference field readers."""

from __future__ import annotations

import types
import unittest
from unittest import mock

from core.parse import reference_readers as rr
from core.parse import parsing_common as pc


class TestReferenceFieldReaders(unittest.TestCase):
    def test_vancouver_reader_extracts_fields_and_exact_spans(self):
        raw = "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised trials. Lancet 365: 1159."
        result = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(result["reader"], "vancouver-fields")
        self.assertEqual(result["status"], "parsed")
        fields = result["fields"]
        self.assertEqual(fields["authors"]["value"], ["Chan AW", "Altman DG"])
        self.assertEqual(fields["title"]["value"], "Epidemiology and reporting of randomised trials")
        self.assertEqual(fields["journal"]["value"], "Lancet")
        self.assertEqual(fields["year"]["value"], 2005)
        self.assertEqual(fields["volume"]["value"], "365")
        self.assertEqual(fields["pages"]["value"], "1159")
        self.assertTrue(rr.validate_parse(result, raw))

    def test_reference_construction_uses_structural_fields_before_heuristic_title(self):
        raw = "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised trials. Lancet 365: 1159."
        ref = pc._make_reference(1, raw)
        self.assertEqual(ref["title"], "Epidemiology and reporting of randomised trials")
        self.assertEqual(ref["authors"], ["Chan AW", "Altman DG"])
        self.assertEqual(ref["journal"], "Lancet")
        self.assertEqual(ref["year"], 2005)
        self.assertEqual(ref["bibliographic_parse"]["reader"], "vancouver-fields")

    def test_generic_reader_is_explicit_and_does_not_choose_a_year_from_title(self):
        raw = "A 2020 Perspective on Metadata. No closed publication date."
        record = rr.parse_entry(raw)
        self.assertEqual(record["reader"], "generic-text")
        self.assertEqual(record["status"], "parsed")
        self.assertNotIn("year", record["fields"])

    def test_semicolon_and_dated_book_readers_extract_specific_structures(self):
        journal = "Novotny, L.; van Hulst, N. Antennas for light. Nature Photonics 2011, 5, 83-90."
        parsed = rr.parse_entry(journal, include_generic=False)
        self.assertEqual(parsed["reader"], "semicolon-journal")
        self.assertEqual(parsed["fields"]["journal"]["value"], "Nature Photonics")
        self.assertEqual(parsed["fields"]["pages"]["value"], "83-90")
        book = ("Andersson, Ruben 2014 Illegality, Inc.: Clandestine Migration and the "
                "Business of Bordering Europe. Berkeley: University of California Press.")
        parsed = rr.parse_entry(book, include_generic=False)
        self.assertEqual(parsed["reader"], "dated-book")
        self.assertEqual(parsed["fields"]["title"]["value"],
                         "Illegality, Inc.: Clandestine Migration and the Business of Bordering Europe")
        self.assertEqual(parsed["fields"]["first_author"]["value"], "Andersson")
        self.assertEqual(parsed["fields"]["place"]["value"], "Berkeley")
        self.assertEqual(parsed["fields"]["publisher"]["value"], "University of California Press")

    def test_semicolon_author_with_multiple_initials_does_not_become_one_letter_title(self):
        raw = ("Kauranen, M.; Zayats, A. V. Nonlinear plasmonics. "
               "Nature Photonics 2012, 6, 737−748.")
        record = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(record["reader"], "semicolon-journal")
        self.assertEqual(record["fields"]["authors"]["value"],
                         ["Kauranen, M.", "Zayats, A. V."])
        self.assertEqual(record["fields"]["title"]["value"], "Nonlinear plasmonics")
        self.assertEqual(record["fields"]["journal"]["value"], "Nature Photonics")

    def test_dated_chapter_keeps_anchored_year_but_abstains_on_combined_title(self):
        raw = ("Martin, David A. 1988 Introduction to The New Asylum Seekers: Refugee Law in the "
               "1980s; The Ninth Sokol Colloquium on International Law, edited by David Martin, "
               "1–20. New York: Springer.")
        record = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(record["reader"], "dated-book")
        self.assertEqual(record["status"], "parsed")
        self.assertEqual(record["fields"]["year"]["value"], 1988)
        self.assertNotIn("title", record["fields"])
        self.assertEqual(record["fields"]["place"]["value"], "New York")
        self.assertEqual(record["fields"]["publisher"]["value"], "Springer")
        ref = pc._make_reference(1, raw)
        self.assertIsNone(ref["title"])
        self.assertEqual(ref["year"], 1988)

        ordinary_book = ("Martin, David A. 1988 The New Asylum Seekers: Refugee Law; "
                         "A Standalone Subtitle. New York: Springer.")
        control = rr.parse_entry(ordinary_book, include_generic=False)
        self.assertEqual(control["fields"]["title"]["value"],
                         "The New Asylum Seekers: Refugee Law; A Standalone Subtitle")

    def test_structural_line_boundaries_split_mixed_wrapped_entries_only_when_complete(self):
        mixed = "\n".join((
            "Kauranen, M.; Zayats, A. V. Antennas for light.",
            "Nature Photonics 2011, 5, 83-90.",
            "Smith, J. (2020). A clear title. Journal of Tests, 5(1), 12-34. "
            "https://doi.org/10.5555/test.",
        ))
        with (mock.patch.object(pc, "_split_authoryear_references", return_value=[mixed]),
              mock.patch.object(pc, "_split_hanging_authoryear_references", return_value=None)):
            refs = pc.default_parse_references(mixed)
        self.assertEqual(len(refs), 2)
        self.assertEqual([ref["bibliographic_parse"]["reader"] for ref in refs],
                         ["semicolon-journal", "apa-journal"])
        self.assertEqual(refs[0]["title"], "Antennas for light")
        self.assertEqual(refs[1]["doi"], "10.5555/test")

        ambiguous_abbreviated = "\n".join((
            "Kauranen, M.; Zayats, A. V. Antennas for light.",
            "Nat. Photonics 2011, 5, 83-90.",
        ))
        self.assertIsNone(pc._split_structural_line_entries(ambiguous_abbreviated))

        incomplete = mixed + "\nBrown, R. (2021). An incomplete author-like continuation."
        self.assertIsNone(pc._split_structural_line_entries(incomplete))

        vancouver = "\n".join((
            "Chan AW (2005) First complete article title. Lancet 365: 1159.",
            "Moher D (2010) Second complete article title. BMJ 340: c869.",
            "Brown, R. (2021). An incomplete author-like continuation.",
        ))
        self.assertIsNone(pc._split_structural_line_entries(vancouver))

    def test_apa_shape_and_single_author_compound_vancouver_title(self):
        apa = "Smith, J. (2020). A clear title. Journal of Tests, 5(1), 12-34."
        parsed = rr.parse_entry(apa, include_generic=False)
        self.assertEqual(parsed["reader"], "apa-journal")
        self.assertEqual(parsed["fields"]["first_author"]["value"], "Smith")
        compound = ("Rennie D (1995) Reporting randomized controlled trials. An experiment "
                    "and a call for responses from readers. JAMA 273: 1054-1055.")
        ref = pc._make_reference(1, compound)
        self.assertEqual(ref["bibliographic_parse"]["reader"], "vancouver-fields")
        self.assertEqual(ref["authors"], ["Rennie D"])
        self.assertEqual(ref["title"], "Reporting randomized controlled trials. An experiment and a call for responses from readers")
        self.assertEqual(ref["journal"], "JAMA")
        self.assertEqual(ref["year"], 1995)

    def test_vancouver_sentence_title_is_not_swallowed_as_journal(self):
        raw = (
            "Rennie D (1995) A JAMA study. A second sentence about reporting. "
            "JAMA 273: 1054–1055."
        )
        parsed = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["fields"]["title"]["value"],
                         "A JAMA study. A second sentence about reporting")
        self.assertEqual(parsed["fields"]["journal"]["value"], "JAMA")
        self.assertTrue(rr.validate_parse(parsed, raw))

    def test_vancouver_abbreviated_venue_prefix_is_not_certified_as_title(self):
        raw = "Smith AB (2020) A clear article title. J. Clin. Oncol. 38: 12-34."
        parsed = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(parsed["status"], "ambiguous")
        self.assertEqual(parsed["fields"], {})
        self.assertIsNone(pc._make_reference(1, raw)["title"])
        self.assertTrue(rr.validate_parse(parsed, raw))

    def test_bluebook_rejects_prefix_of_comma_only_author_list(self):
        raw = (
            "Li Fang Si, MengYun Li, Lei He,(2022), Farmland monitoring and "
            "livestock management based on internet of things, Internet of "
            "Things,Volume 19,100581."
        )
        parsed = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(parsed["status"], "unrecognized")

    def test_explicit_edition_proves_book_but_not_ambiguous_comma_prefix_title(self):
        raw = "John Doe, Jane Roe, A Collaborative Work (2d ed. 2021)."
        parsed = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(parsed["status"], "parsed")
        self.assertEqual(parsed["classification"]["source_kind"], "book_like")
        self.assertNotIn("title", parsed["fields"])
        self.assertEqual(parsed["fields"]["year"]["value"], 2021)
        self.assertTrue(rr.validate_parse(parsed, raw))
        self.assertIsNone(pc._make_reference(1, raw)["title"])

    def test_ambiguous_multiple_editions_do_not_supply_a_book_year(self):
        raw = "John Doe, Jane Roe, A Collaborative Work (2d ed. 2021); revised (3d ed. 2023)."
        parsed = rr.parse_entry(raw)
        self.assertEqual(parsed["status"], "ambiguous")
        self.assertEqual(parsed["fields"], {})
        self.assertIsNone(pc._make_reference(1, raw)["year"])

    def test_compact_initialized_article_title_proof_ends_before_journal(self):
        raw = (
            "Roldán, M. D., E. Pérez-Reinado, F. Castillo, and C. Moreno-Vivián "
            "(2008) Reduction of polynitroaromatic compounds: the bacterial "
            "nitroreductases. FEMS Microbiol. Rev. 32: 474–500."
        )
        ref = pc._make_reference(1, raw)
        self.assertEqual(ref["title"],
                         "Reduction of polynitroaromatic compounds: the bacterial nitroreductases")
        proof = ref["bibliographic_parse"]["fields"]["title"]
        span = proof["spans"][0]
        self.assertEqual(raw[span["start"]:span["end"]], ref["title"])
        self.assertTrue(rr.validate_parse(ref["bibliographic_parse"], raw))

    def test_bluebook_year_comes_from_publication_anchor(self):
        raw = ("See Molly Land, Toward an International Law of the Internet 2020, "
               "54 Harv. Int’l L.J. 393, 394 (2013).")
        record = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(record["reader"], "bluebook")
        self.assertEqual(record["fields"]["year"]["value"], 2013)
        ref = pc._make_reference(1, raw)
        self.assertIn("2020", ref["title"])
        self.assertEqual(ref["year"], 2013)

    def test_bluebook_year_comes_from_closed_reporter_spine(self):
        cases = (
            ("See Matthew B. Lawrence, Public Health Law’s Digital Frontier: Addictive Design, Section 230, and the Freedom of Speech, 2024 J. Free Speech L. 299, 317.", 2024),
            ("Meg Leta Jones, Does Technology Drive Law? The Dilemma of Technological Exceptionalism in Cyberlaw, 2018 U. Ill. J.L. Tech. & Pol’y 249, 251.", 2018),
            ("See Joel Margolis, Tae (Tom) Oh, Suyash Jadhav, Young Ho Kim & Jeong Neyo Kim, An In- Depth Analysis of the Mirai Botnet, 2017 Int’l Conf. on Software Sec. & Assurance 6, 6.", 2017),
            ("See Ann L. Hollick, The Origins of 200-Mile Offshore Zones, 71 Am. J. Int’l L. 494, 494 & n.3 (1977).", 1977),
        )
        for raw, expected_year in cases:
            with self.subTest(year=expected_year):
                parsed = rr.parse_entry(raw, include_generic=False)
                self.assertEqual(parsed["reader"], "bluebook")
                self.assertEqual(parsed["fields"]["year"]["value"], expected_year)
                self.assertTrue(rr.validate_parse(parsed, raw))
                span = parsed["fields"]["year"]["spans"][0]
                self.assertEqual(raw[span["start"]:span["end"]], str(expected_year))

    def test_ambiguous_structural_reader_does_not_infer_a_year(self):
        reader = types.SimpleNamespace(
            NAME="uncertain-year", VERSION="1", ORDER=1, STRUCTURAL=True,
            parse_fields=lambda _entry: {
                "reader": "uncertain-year", "version": "1", "status": "ambiguous",
                "fields": {}, "conflicts": [{"field": "year", "reason": "no anchor"}],
            },
        )
        with mock.patch.object(rr, "get_registry", return_value=[reader]):
            ref = pc._make_reference(1, "A 2020 Perspective on a 1980s archive.")
        self.assertEqual(ref["bibliographic_parse"]["status"], "ambiguous")
        self.assertIsNone(ref["year"])

    def test_multiple_structural_claims_abstain_regardless_of_order(self):
        raw = "citation"
        def reader(name):
            def parse(_entry):
                return {"reader": name, "version": "1", "status": "parsed",
                        "fields": {"title": {"value": "citation" if name == "one" else "different", "spans": [
                            {"start": 0, "end": 8, "raw_value": "citation"}], "rule": "test"}},
                        "conflicts": []}
            return types.SimpleNamespace(NAME=name, ORDER=1, STRUCTURAL=True, parse_fields=parse)
        with mock.patch.object(rr, "get_registry", return_value=[reader("one"), reader("two")]):
            result = rr.parse_entry(raw, include_generic=False)
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["fields"], {})

    def test_recognized_ambiguity_does_not_fall_through_to_generic(self):
        ambiguous = types.SimpleNamespace(
            NAME="specific", ORDER=1, STRUCTURAL=True,
            parse_fields=lambda _entry: {"reader": "specific", "version": "1",
                "status": "ambiguous", "fields": {},
                "conflicts": [{"field": "title", "reason": "unclear boundary"}]},
        )
        generic = types.SimpleNamespace(
            NAME="generic", ORDER=100, GENERIC=True, STRUCTURAL=False,
            parse_fields=lambda *_args, **_kwargs: self.fail("generic reader must not run"),
        )
        with mock.patch.object(rr, "get_registry", return_value=[ambiguous, generic]):
            result = rr.parse_entry("citation")
        self.assertEqual(result["status"], "ambiguous")

    def test_validation_rejects_bad_source_spans(self):
        record = {"reader": "x", "version": "1", "status": "parsed", "conflicts": [],
                  "fields": {"title": {"value": "wrong", "rule": "test",
                      "spans": [{"start": 0, "end": 3, "raw_value": "bad"}]}}}
        self.assertFalse(rr.validate_parse(record, "good"))

    def test_apply_readers_rejects_malformed_proof_before_pruning_enriched_value(self):
        raw = "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised trials. Lancet 365: 1159."
        ref = pc._make_reference(1, raw)
        title = ref["bibliographic_parse"]["fields"]["title"]
        title["value"] = "Enriched elsewhere"
        title["spans"][0]["raw_value"] = "not the source span"
        with self.assertRaisesRegex(ValueError, "bibliographic Parse evidence is invalid"):
            pc.apply_reference_readers([ref])

    def test_apply_readers_prunes_valid_raw_proof_after_enrichment(self):
        raw = "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised trials. Lancet 365: 1159."
        ref = pc._make_reference(1, raw)
        ref["title"] = "Enriched elsewhere"
        pc.apply_reference_readers([ref])
        self.assertEqual(ref["title"], "Enriched elsewhere")
        self.assertNotIn("title", ref["bibliographic_parse"]["fields"])

    def test_mixed_bibliography_attempts_minority_style_per_entry(self):
        raw = "Chan AW, Altman DG (2005) Epidemiology and reporting of randomised trials. Lancet 365: 1159."
        self.assertEqual(rr.parse_entry(raw, include_generic=False)["reader"], "vancouver-fields")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
