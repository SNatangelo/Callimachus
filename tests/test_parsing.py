#!/usr/bin/env python3
# tests/test_parsing.py
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only
"""Parsing and citation-linking tests."""
from tests._bootstrap import *  # noqa: F401,F403

class TestAuthorYear(unittest.TestCase):
    def test_detect_and_stoplist(self):
        self.assertEqual(len(authoryear.find_intext("Progress here (Smith 2021).")), 1)
        self.assertEqual(authoryear.find_intext("It happened (In 2021) somewhere."), [])
        two = authoryear.find_intext("Debate (Brown 2020a; Brown 2020b) continues.")
        self.assertEqual(len(two), 2)
        self.assertEqual({c["suffix"] for c in two}, {"a", "b"})

    def test_narrative_form(self):
        cites = authoryear.find_intext("Smith et al. (2019) argued otherwise.")
        self.assertEqual(len(cites), 1)
        self.assertEqual(cites[0]["surname"], "smith")
        self.assertEqual(cites[0]["year"], 2019)

    def test_compound_and_institutional_author_year(self):
        cases = [
            ("According to Transparency International (2023), x.", "transparency", 2023),
            ("This holds (Research Journal of Business and Management, 2021).", "research", 2021),
            ("Data show this (Ethi- opian Ministry of Education, 2022).", "ethiopian", 2022),
            ("Prior work agrees (Vande- kerckhove & Commers, 2020).", "vandekerckhove", 2020),
            ("Blenkin- sopp and Edwards (2008) reviewed this.", "blenkinsopp", 2008),
            ("RETRACTED ARTICLE Kassa and Tsegaye (2022) reported this.", "kassa", 2022),
        ]
        for text, surname, year in cases:
            with self.subTest(text=text):
                cites = authoryear.find_intext(text)
                self.assertEqual(len(cites), 1)
                self.assertEqual(cites[0]["surname"], surname)
                self.assertEqual(cites[0]["year"], year)

    def test_journal_header_is_not_author_year_citation(self):
        cites = authoryear.find_intext("Journal of Academic Ethics (2025) 23:1829-1846.")
        self.assertEqual(cites, [])

    def test_entry_key_and_match(self):
        refs = [
            {"id": "r1", "ref_number": 1, "raw_entry": "Lee, X. 2018. One paper.",
             "ay_surname": "lee", "ay_year": 2018, "ay_suffix": ""},
            {"id": "r2", "ref_number": 2, "raw_entry": "Lee, Y. 2018. Another paper.",
             "ay_surname": "lee", "ay_year": 2018, "ay_suffix": ""},
            {"id": "r3", "ref_number": 3, "raw_entry": "Smith, J. 2021. Things.",
             "ay_surname": "smith", "ay_year": 2021, "ay_suffix": ""},
        ]
        sur, yr, suf = authoryear.entry_key("Smith, John. 2021a. A study.")
        self.assertEqual((sur, yr, suf), ("smith", 2021, "a"))
        idx = authoryear.build_index(refs)
        self.assertEqual(authoryear.match({"surname": "smith", "year": 2021, "suffix": ""}, idx)[0],
                         "unique")
        self.assertEqual(authoryear.match({"surname": "lee", "year": 2018, "suffix": ""}, idx)[0],
                         "ambiguous")
        self.assertEqual(authoryear.match({"surname": "ghost", "year": 1999, "suffix": ""}, idx)[0],
                         "orphan")

    def test_match_disambiguates_single_author_vs_et_al(self):
        # Two works with the same (surname, year) and no a/b/c suffix, told apart
        # by author count: a bare-surname cite is the single-author entry, an
        # "et al." cite the multi-author one.
        refs = [
            {"id": "r1", "ref_number": 1, "raw_entry": "Jackson, M. (2024), Solo study.",
             "ay_surname": "jackson", "ay_year": 2024, "ay_suffix": ""},
            {"id": "r2", "ref_number": 2,
             "raw_entry": "Jackson, M., Von Dohlen, H. and Black-Chen, M. (2024), Team study.",
             "ay_surname": "jackson", "ay_year": 2024, "ay_suffix": ""},
        ]
        idx = authoryear.build_index(refs)
        bare = authoryear.match(
            {"surname": "jackson", "year": 2024, "suffix": "",
             "marker_raw": "(Jackson, 2024)", "coauthors": []}, idx)
        self.assertEqual((bare[0], bare[1]["ref_number"]), ("unique", 1))
        etal = authoryear.match(
            {"surname": "jackson", "year": 2024, "suffix": "",
             "marker_raw": "(Jackson et al., 2024)", "coauthors": []}, idx)
        self.assertEqual((etal[0], etal[1]["ref_number"]), ("unique", 2))

    def test_apostrophe_surname_normalized_and_segmented(self):
        from core.parse.parse_manuscript import normalize
        from core.parse.parsing_common import _split_authoryear_references
        # Every apostrophe variant folds to a single ASCII "'".
        for variant in ("O’Malley", "OʼMalley", "O＇Malley"):
            self.assertEqual(normalize(variant), "O'Malley")
        # The apostrophe surname reads as one key.
        self.assertEqual(
            authoryear.entry_key("O'Malley, M.P. and Capper, C.A. (2015), A measure.")[:2],
            ("o'malley", 2015))
        # An apostrophe-surname entry on its own line segments as its own
        # reference (previously it was swallowed by the preceding entry).
        biblio = normalize(
            "North Carolina State Board (2006), Standards, Raleigh, NC.\n"
            "O’Malley, M.P. and Capper, C.A. (2015), A measure. EAQ, 51(2), 290-330.\n"
            "Roegman, R. (2021), Complexity. JEA.")
        keys = [authoryear.entry_key(r)[:2] for r in _split_authoryear_references(biblio)]
        self.assertIn(("o'malley", 2015), keys)

    def test_normalize_replaces_nul_with_replacement_character(self):
        from core.parse.parse_manuscript import normalize

        self.assertEqual(normalize("formula\x00text"), "formula\uFFFDtext")

    def test_match_two_single_author_works_stay_ambiguous(self):
        # The rule must not guess when it cannot uniquely pick: two single-author
        # works with the same key remain ambiguous for a bare-surname cite.
        refs = [
            {"id": "r1", "ref_number": 1, "raw_entry": "Jackson, M. (2024), Solo A.",
             "ay_surname": "jackson", "ay_year": 2024, "ay_suffix": ""},
            {"id": "r2", "ref_number": 2, "raw_entry": "Jackson, M. (2024), Solo B.",
             "ay_surname": "jackson", "ay_year": 2024, "ay_suffix": ""},
        ]
        kind, _ = authoryear.match(
            {"surname": "jackson", "year": 2024, "suffix": "",
             "marker_raw": "(Jackson, 2024)", "coauthors": []},
            authoryear.build_index(refs))
        self.assertEqual(kind, "ambiguous")

    def test_match_et_al_excludes_the_two_author_entry(self):
        # BERT's real collision: two "Clark 2018" entries, one with two authors and
        # one with four.  "et al." is reserved for three authors or more — a
        # two-author work is always cited naming both — so "Clark et al." can only
        # be the four-author entry, and "Clark and Gardner" only the two-author one.
        # The old single/multi split called both entries "multi" and gave up.
        refs = [
            {"id": "r11", "ref_number": 11, "ay_surname": "clark", "ay_year": 2018,
             "ay_suffix": "", "raw_entry": "Christopher Clark and Matt Gardner. 2018. "
                                           "Simple and effective multi-paragraph reading "
                                           "comprehension. In ACL."},
            {"id": "r12", "ref_number": 12, "ay_surname": "clark", "ay_year": 2018,
             "ay_suffix": "", "raw_entry": "Kevin Clark, Minh-Thang Luong, Christopher D "
                                           "Manning, and Quoc Le. 2018. Semi-supervised "
                                           "sequence modeling with cross-view training."},
        ]
        idx = authoryear.build_index(refs)
        etal = authoryear.match(
            {"surname": "clark", "year": 2018, "suffix": "",
             "marker_raw": "(Clark et al., 2018)", "coauthors": []}, idx)
        self.assertEqual((etal[0], etal[1]["ref_number"]), ("unique", 12))
        named = authoryear.match(
            {"surname": "clark", "year": 2018, "suffix": "",
             "marker_raw": "(Clark and Gardner, 2018)", "coauthors": ["gardner"]}, idx)
        self.assertEqual((named[0], named[1]["ref_number"]), ("unique", 11))

    def test_match_et_al_stays_ambiguous_when_both_entries_are_multi_author(self):
        # Attention's "Luong et al. (2015)": both candidates have three or more
        # authors, so "et al." cannot separate them.  Guessing here would fabricate
        # a source, so the rule must still refuse.
        refs = [
            {"id": "r23", "ref_number": 23, "ay_surname": "luong", "ay_year": 2015,
             "ay_suffix": "", "raw_entry": "Minh-Thang Luong, Quoc V. Le, Ilya Sutskever, "
                                           "Oriol Vinyals, and Lukasz Kaiser. 2015. "
                                           "Multi-task sequence to sequence learning."},
            {"id": "r24", "ref_number": 24, "ay_surname": "luong", "ay_year": 2015,
             "ay_suffix": "", "raw_entry": "Minh-Thang Luong, Hieu Pham, and Christopher D "
                                           "Manning. 2015. Effective approaches to "
                                           "attention-based neural machine translation."},
        ]
        kind, group = authoryear.match(
            {"surname": "luong", "year": 2015, "suffix": "",
             "marker_raw": "Luong et al. (2015)", "coauthors": []},
            authoryear.build_index(refs))
        self.assertEqual(kind, "ambiguous")
        self.assertEqual(sorted(r["ref_number"] for r in group), [23, 24])

    def test_match_et_al_for_a_two_author_work_still_resolves(self):
        # An author who writes "et al." for a two-author work is wrong but common.
        # The strict rule finds no three-author candidate; the coarser single/multi
        # split must still catch it, so the new rule only ever ADDS resolutions.
        refs = [
            {"id": "r1", "ref_number": 1, "ay_surname": "jackson", "ay_year": 2024,
             "ay_suffix": "", "raw_entry": "Jackson, M. (2024), Solo study."},
            {"id": "r2", "ref_number": 2, "ay_surname": "jackson", "ay_year": 2024,
             "ay_suffix": "", "raw_entry": "Jackson, M. and Black-Chen, M. (2024), Pair study."},
        ]
        got = authoryear.match(
            {"surname": "jackson", "year": 2024, "suffix": "",
             "marker_raw": "(Jackson et al., 2024)", "coauthors": []},
            authoryear.build_index(refs))
        self.assertEqual((got[0], got[1]["ref_number"]), ("unique", 2))

    def test_nature_ampersand_coauthor_does_not_hide_the_title(self):
        from core.parse import parsing_common
        # Nature style joins the last co-author with "&". The period after the
        # preceding initial is not masked (an "&" follows, not a capital), so the
        # entry splits there and the part holding the real title starts with the
        # trailing co-author. Rejecting that part wholesale used to leave the
        # journal abbreviation as the title — "Antimicrob. Agents Chemother."
        # became "Agents Chemother", and the resolver then searched on a journal
        # name.
        self.assertEqual(
            parsing_common._extract_title(
                "Castillo-Ramírez, S., Aguilar-Vera, A., Kumar, A. & Evans, B. "
                "Acinetobacter baumannii: much more than a human pathogen. "
                "Antimicrob. Agents Chemother. 69, e00801–e00825 (2025)."),
            "Acinetobacter baumannii: much more than a human pathogen",
        )
        # The same shape returned no title at all, which is worse: nothing to
        # search on. This entry is the fixture already used by the author-count
        # test below, where the title was never asserted.
        self.assertEqual(
            parsing_common._extract_title(
                "Jain, C., Rodriguez-R, L. M., Phillippy, A. M., "
                "Konstantinidis, K. T. & Aluru, S. High throughput ANI analysis "
                "of 90K prokaryotic genomes reveals clear species boundaries. "
                "Nat. Commun. 9, 5114 (2018)."),
            "High throughput ANI analysis of 90K prokaryotic genomes reveals "
            "clear species boundaries",
        )
        # An "&" that is genuine debris keeps being rejected: the strip requires
        # a real initial after the surname, which a publisher name has not.
        self.assertFalse(parsing_common._is_title_fragment("& Sons, Publishers of note"))

    def test_vancouver_undotted_initials_keep_compound_title(self):
        from core.parse import parsing_common
        self.assertEqual(
            parsing_common._extract_title(
                "Monici M, Cialdai F, Fusi F, Romano G, Pratesi R. Effects of "
                "pulsed Nd:YAG laser at molecular and cellular level. A study on "
                "the basis of Hilterapia. Energy Health. (2009) 3:26–33."
            ),
            "Effects of pulsed Nd:YAG laser at molecular and cellular level. "
            "A study on the basis of Hilterapia",
        )

    def test_a_word_the_pdf_broke_across_lines_is_rejoined_in_the_title(self):
        """Text extraction leaves "classi- fiers" where the PDF wrapped a word.

        Carried into the title it defeats every lookup — no index holds
        "train strong classi- fiers". 73 of the corpus's extracted titles carry one.
        """
        from core.parse import parsing_common
        self.assertEqual(
            parsing_common._extract_title(
                "Yun, S., Han, D., Oh, S. J., Chun, S., Choe, J. & Yoo, Y. "
                "Cutmix: Regularization strategy to train strong classi- fiers "
                "with localizable features. ICCV 6023–6032 (2019)."),
            "Cutmix: Regularization strategy to train strong classifiers with "
            "localizable features",
        )

    def test_a_capital_after_the_break_keeps_its_hyphen(self):
        """"Third- Party" is a real compound that merely fell at a line break.

        Requiring lowercase on both sides is what separates the artefact from a
        hyphen the title actually has.
        """
        from core.parse import parsing_common
        self.assertEqual(
            parsing_common._join_soft_hyphen_breaks(
                "A Global Assessment of Third- Party Connection Tampering"),
            "A Global Assessment of Third- Party Connection Tampering",
        )
        self.assertEqual(
            parsing_common._join_soft_hyphen_breaks("net- work infra- structure"),
            "network infrastructure",
        )

    def test_entry_author_count_reads_names_not_punctuation(self):
        count = authoryear._entry_author_count
        # Nature style, year LAST: the author list ends where the title's prose
        # begins, otherwise the title and the journal are counted as authors.
        self.assertEqual(count("Jain, C., Rodriguez-R, L. M., Phillippy, A. M., "
                               "Konstantinidis, K. T. & Aluru, S. High throughput ANI "
                               "analysis of 90,000 prokaryotic genomes reveals clear "
                               "species boundaries. Nat. Commun. 9, 5114 (2018)."), 5)
        # Bare year, and a title carrying an "and": the author block ends at the
        # year, so the title cannot invent a second author.
        self.assertEqual(count("Christopher Clark and Matt Gardner. 2018. Simple and "
                               "effective multi-paragraph reading comprehension."), 2)
        self.assertEqual(count("Kevin Clark, Minh-Thang Luong, Christopher D Manning, "
                               "and Quoc Le. 2018. Semi-supervised sequence modeling."), 4)
        self.assertEqual(count("Jackson, M. (2024), Solo study."), 1)
        # A lower-case particle stays inside the name it belongs to.
        self.assertEqual(count("Olivier J. Henaff, Aravind Srinivas, and Aaron van den "
                               "Oord. 2020. Data-efficient image recognition."), 3)
        # An entry that abbreviates its own author list is three or more by definition.
        self.assertEqual(count("Page, A. J. et al. Roary: rapid pan genome analysis. (2015)."), 3)

    def test_parse_authoryear_endtoend(self):
        md = ("# P\n\nClaim one (Smith 2021). Two here (Lee 2018).\n\n## References\n\n"
              "Smith, J. 2021. Things. Journal.\nLee, X. 2018. A. Journal.\n"
              "Lee, Y. 2018. B. Journal.\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            with open(p, "w") as f:
                f.write(md)
            parsed, _dbg = parse(p, 1, "auto")
            self.assertEqual(parsed["manuscript"]["citation_mode"], "author-year")
            self.assertEqual(len(parsed["ambiguities"]), 1)         # Lee 2018 is ambiguous
            # Smith 2021 uniquely linked
            linked = [c for c in parsed["citations"] if c.get("ref_id")]
            self.assertTrue(any(c["ref_number"] == 1 for c in linked))

    def test_multiline_authoryear_references_are_grouped(self):
        from core.parse.parse_manuscript import _parse_references
        biblio = (
            "Abebe, T., & Tesfaye, M. (2021). Disability and institutional barriers. Journal\n"
            "of Ethiopian Studies, 45(2), 123-140.\n"
            "Baker, R., & McCafferty, D. (2020). The role of organizational culture: A systematic\n"
            "RETRACTED ARTICLE\n"
            "review. Journal of Business Ethics, 170(3), 567-582.\n"
            "1845\n"
            "Ethiopian Ministry of Education (2022). Annual report on disability inclusion.\n"
            "Addis Ababa: Ethiopian Ministry of Education.\n"
            "Mulualem Walle, Y. (2020). The impact of digital government on whistle-blower protec-\n"
            "tion: Explanatory study. Journal of Information Technology Management, 12(1), 1-26.\n"
            "Publisher's Note Springer Nature remains neutral with regard to jurisdictional claims.\n"
            "Authors and Affiliations\n"
        )
        refs = _parse_references(biblio)
        self.assertEqual(len(refs), 4)
        self.assertIn("Journal of Ethiopian Studies", refs[0]["raw_entry"])
        self.assertIn("systematic review", refs[1]["raw_entry"])
        self.assertEqual(refs[2]["raw_entry"].split(".")[0],
                         "Ethiopian Ministry of Education (2022)")
        self.assertIn("whistle-blower protection", refs[3]["raw_entry"])
        self.assertNotIn("Publisher", refs[-1]["raw_entry"])


class TestReferenceClassification(unittest.TestCase):
    def test_reference_kind_and_indexability(self):
        from core.parse.parse_manuscript import _make_reference
        article = _make_reference(
            1, "Smith, J. (2020). A study. Journal of Tests, 12(3), 45-60.")
        book = _make_reference(
            2, "Krueger, R. A. (2015). Focus groups: A practical guide (5th ed.). SAGE.")
        report = _make_reference(
            3, "Transparency International (2023). Corruption Perceptions Index 2023. Retrieved from https://example.org")
        self.assertEqual(article["source_kind"], "article_like")
        self.assertEqual(article["indexability"], "high")
        self.assertEqual(book["source_kind"], "book_like")
        self.assertEqual(book["indexability"], "low")
        self.assertEqual(report["source_kind"], "report_like")
        self.assertIn(report["indexability"], ("medium", "low"))

    def test_reference_work_doi_is_not_a_high_index_article(self):
        # OUP reference-work DOIs (here MPEPIL law:epil) are encyclopedia entries,
        # not journal articles; a Crossref miss must not read as a missing
        # high-index article. A normal journal DOI still classifies article/high.
        from core.parse.parsing_common import _classify_by_text
        rw = _classify_by_text(
            "State Succession in Treaties, MPEPIL (2019).",
            doi="10.1093/law:epil/9780199231690/law-9780199231690-e1109")
        self.assertEqual(rw["source_kind"], "chapter_like")
        self.assertEqual(rw["indexability"], "medium")
        normal = _classify_by_text(
            "Smith J. A study. J Tests. 2020.", doi="10.1000/xyz123")
        self.assertEqual(normal["source_kind"], "article_like")
        self.assertEqual(normal["indexability"], "high")

    def test_reference_title_extraction_ignores_author_initial_periods(self):
        from core.parse.parse_manuscript import _make_reference
        ref = _make_reference(
            23,
            (
                "Minh-Thang Luong, Quoc V. Le, Ilya Sutskever, Oriol Vinyals, and Lukasz Kaiser. "
                "Multi-task sequence to sequence learning. arXiv preprint arXiv:1511.06114, 2015."
            ),
        )
        self.assertEqual(ref["title"], "Multi-task sequence to sequence learning")
        self.assertEqual(ref["doi"], "10.48550/arXiv.1511.06114")
        self.assertEqual(ref["year"], 2015)

    def test_url_host_split_around_a_dot_is_rejoined(self):
        """A wrap either side of a dot in the host truncated the URL to a bare
        "https://www" / "https://avalon.law.yale", and the fetch then failed on a
        host that does not exist."""
        from core.parse.parse_manuscript import _make_reference
        after = _make_reference(
            1, "Davos 2026 (Jan. 20, 2026), https://www. weforum.org/stories/2026/01/davos")
        self.assertEqual(after["url"],
                         "https://www.weforum.org/stories/2026/01/davos")
        before = _make_reference(
            1, "reprinted by Avalon Project, https://avalon.law.yale .edu/18th_century/sp1795n.asp")
        self.assertEqual(before["url"],
                         "https://avalon.law.yale.edu/18th_century/sp1795n.asp")

    def test_url_split_mid_path_before_a_filename_is_rejoined(self):
        """A wrap fell mid-path, not around a dot, so the host rules could not see
        it: the URL truncated at the injected space and the fetch retrieved the
        wrong (or a non-existent) document.  The tail is unmistakably the rest of
        the link — no space and a document extension — so it is rejoined."""
        from core.parse.parse_manuscript import _make_reference
        # ref#205 (UN Treaty Collection): "…/LON/PA RTII-16.en.pdf" truncated to
        # "…/LON/PA"; the continuation is capitalised and carries no dotted TLD.
        un = _make_reference(
            1,
            "Convention and Statute on Freedom of Transit, U.N. Treaty Collection 2, "
            "https://treaties.un.org/doc/Publication/MTDSG/Volume%20II/LON/PA "
            "RTII-16.en.pdf [https://perma.cc/E5ZW-LF7R].")
        self.assertEqual(
            un["url"],
            "https://treaties.un.org/doc/Publication/MTDSG/Volume%20II/LON/PARTII-16.en.pdf")
        # ref#363 (ITU history): "search.itu. int/…/x.pdf" truncated to
        # "https://search.itu"; the continuation is a bare TLD before the path.
        itu = _make_reference(
            1,
            "Paris, 1865: The Birth of the Union, Int'l Telecomm. Union 26 (2015), "
            "https://search.itu. int/history/HistoryDigitalCollectionDocLibrary/"
            "12.36.72.en.100.pdf [https://perma.cc/PFB4-FTEG].")
        self.assertEqual(
            itu["url"],
            "https://search.itu.int/history/HistoryDigitalCollectionDocLibrary/"
            "12.36.72.en.100.pdf")
        # The prose guard still holds: a filename-less tail after a URL-final full
        # stop is the next sentence, not the rest of the link.
        prose = _make_reference(1, "https://example.org/foo. See the report")
        self.assertEqual(prose["url"], "https://example.org/foo")

    def test_justification_hyphen_is_dropped_in_the_host_but_kept_in_the_path(self):
        """"www.ac- cessnow.org" is accessnow.org; "…/northern-ontario- 1101895"
        is one slug.  The same "- " means opposite things either side of the
        first "/", so only the host is repaired."""
        from core.parse.parse_manuscript import _make_reference
        host = _make_reference(
            1, "Access Now, https://www.ac- cessnow.org/campaign/keepiton [perma]")
        self.assertEqual(host["url"], "https://www.accessnow.org/campaign/keepiton")
        path = _make_reference(
            1, "https://www.tbnewswatch.com/local-news/ford-northern-ontario- 1101895")
        self.assertEqual(
            path["url"],
            "https://www.tbnewswatch.com/local-news/ford-northern-ontario-1101895")
        # A hyphen that belongs to the domain must survive untouched.
        real = _make_reference(1, "https://my-site.com/page")
        self.assertEqual(real["url"], "https://my-site.com/page")

    def test_a_bracketed_archive_mirror_does_not_keep_its_bracket(self):
        """Bluebook wraps the perma.cc mirror in brackets; the closing one is not
        part of the link, and the fetch fails on it."""
        from core.parse.parse_manuscript import _make_reference
        ref = _make_reference(
            1, "Access Now, KeepItOn [https://perma.cc/4X86-RHZY] (2024).")
        self.assertEqual(ref["url"], "https://perma.cc/4X86-RHZY")

    def test_prose_after_a_url_is_not_swallowed(self):
        """The guard the dotted-TLD requirement exists for."""
        from core.parse.parse_manuscript import _make_reference
        self.assertEqual(
            _make_reference(1, "see https://example.org/foo. Accessed 2020")["url"],
            "https://example.org/foo")
        self.assertEqual(
            _make_reference(1, "https://example.org/foo. see also the report")["url"],
            "https://example.org/foo")

    def test_doi_split_by_wrap_space_after_period_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # A PDF line wrap split the DOI after an internal period:
        # "10.1016/j.ultrasmedbio. 2012.11.015". Without repair the extractor
        # keeps the truncated, unresolvable "10.1016/j.ultrasmedbio".
        ref = _make_reference(
            1,
            (
                "Kuo WH, Jian DW, Wang TG, Wang YC. Neck muscle stiffness quantified "
                "by sonoelastography. Ultrasound Med Biol. (2013) 39:1356-61. "
                "doi: 10.1016/j.ultrasmedbio. 2012.11.015"
            ),
        )
        self.assertEqual(ref["doi"], "10.1016/j.ultrasmedbio.2012.11.015")

    def test_a_separate_uri_after_a_doi_is_not_rejoined_as_a_doi_wrap(self):
        from core.parse.parse_manuscript import _make_reference

        cases = (
            (
                "Invented Author. Synthetic result. Journal of Tests 12(3): 44-50. "
                "doi:10.5555/declared-record. https://publisher.example/item/2021/90817",
                "10.5555/declared-record",
                "https://publisher.example/item/2021/90817",
            ),
            (
                "Invented Author. Synthetic result. Journal of Tests. "
                "doi:10.5555/another-record https://publisher.example/item/2022/12345",
                "10.5555/another-record",
                "https://publisher.example/item/2022/12345",
            ),
            (
                "Invented Author. Synthetic result. doi:10.5555/uppercase-record. "
                "HTTPS://publisher.example/item/2023/67890",
                "10.5555/uppercase-record",
                None,
            ),
            (
                "Invented Author. Synthetic result. doi:10.5555/ftp-record. "
                "ftp://publisher.example/item/2024/987",
                "10.5555/ftp-record",
                None,
            ),
            (
                "Invented Author. Synthetic result. doi:10.5555/continued-record-v2",
                "10.5555/continued-record-v2",
                None,
            ),
        )
        for raw, expected_doi, expected_url in cases:
            with self.subTest(expected_doi=expected_doi):
                ref = _make_reference(1, raw)
                self.assertEqual(ref["doi"], expected_doi)
                self.assertEqual(ref["raw_entry"], raw)
                self.assertEqual(ref["url"], expected_url)

    def test_doi_split_by_wrap_space_after_paren_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # Elsevier DOIs embed a "(YY)" group; a wrap after it leaves
        # "10.1016/S1470-2045(17) 30891-4" -> truncated "10.1016/S1470-2045(17)".
        ref = _make_reference(
            1,
            (
                "Copson ER, Maishman TC, et al. Germline BRCA mutation and outcome in "
                "young-onset breast cancer (POSH). Lancet Oncol. 2018;19(2):169-180. "
                "doi:10.1016/S1470-2045(17) 30891-4"
            ),
        )
        self.assertEqual(ref["doi"], "10.1016/S1470-2045(17)30891-4")

    def test_doi_split_right_after_the_slash_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # The most common break in the corpus: the wrap falls immediately after
        # the slash, so the extractor finds no suffix at all and reports no DOI.
        for raw, expected in (
            ("Salazar N. Mobility. Transit Rev. 2002. doi:10.1525/ tran.2002.11.1.36",
             "10.1525/tran.2002.11.1.36"),
            ("Smith J. Title. Antipode. 2020. https://doi.org/10.1111/ anti.12579",
             "10.1111/anti.12579"),
            ("Jones A. Study. J Ed Adm. 2003. doi:10.1177/ 0013161x03259147.",
             "10.1177/0013161x03259147"),
        ):
            self.assertEqual(_make_reference(1, raw)["doi"], expected, raw)

    def test_doi_registrant_split_by_line_wrap_is_rejoined(self):
        from urllib.parse import unquote
        from core.parse.parse_manuscript import _make_reference

        cases = (
            ("Article. https://doi.org/10.101 6/j.urology.2022.02.004",
             "10.1016/j.urology.2022.02.004"),
            ("Article. https://doi.org/10.1 007/s00292-017-0322-9",
             "10.1007/s00292-017-0322-9"),
            ("Article. doi:10.109 3/icb/32.2.276",
             "10.1093/icb/32.2.276"),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                ref = _make_reference(1, raw)
                self.assertEqual(ref["doi"], expected)
                self.assertEqual(ref["source_type"], "article")
                self.assertEqual(ref["source_kind"], "article_like")
                self.assertEqual(ref["source_type_confidence"], "high")
                self.assertEqual(ref["indexability"], "high")
                self.assertIn("strong_article_identifier", ref["source_type_evidence"])
                if ref["url"]:
                    self.assertEqual(unquote(ref["url"]), f"https://doi.org/{expected}")

    def test_doi_registrant_wrap_requires_explicit_doi_context(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "Measurements were reported as 10.101 6/j.urology.2022.02.004 units.")
        self.assertIsNone(ref["doi"])

    def test_unicode_format_controls_in_doi_url_are_ignored_only_for_extraction(self):
        from core.parse.parse_manuscript import _make_reference

        raw = (
            "Author. Article title. Critical Care Medicine, In press. "
            "https://\u200b doi.\u200borg/\u200b10.\u200b1097/\u200bCCM.\u200b00000\u200b00000\u200b006518"
        )
        ref = _make_reference(1, raw)

        self.assertEqual(ref["doi"], "10.1097/CCM.0000000000006518")
        self.assertEqual(ref["source_kind"], "article_like")
        self.assertEqual(ref["raw_entry"], raw)

    def test_parenthesized_doi_url_uses_complete_identifier(self):
        from urllib.parse import unquote
        from core.parse.parse_manuscript import _make_reference

        expected = "10.1016/S0149-7634(05)80224-6"
        ref = _make_reference(
            1, "Article. https://doi.org/10.1016/S0149-7634(05) 80224-6")
        self.assertEqual(ref["doi"], expected)
        self.assertEqual(unquote(ref["url"]), f"https://doi.org/{expected}")

    def test_doi_url_is_not_rebuilt_from_a_different_identifier(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "Article. doi:10.1234/first; https://doi.org/10.1234/second")
        self.assertEqual(ref["doi"], "10.1234/first")
        self.assertEqual(ref["url"], "https://doi.org/10.1234/second")

    def test_truncated_doi_url_is_not_completed_from_another_occurrence(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "First work 10.1234/first. Second link https://doi.org/10.1234/fi")
        self.assertEqual(ref["doi"], "10.1234/first")
        self.assertEqual(ref["url"], "https://doi.org/10.1234/fi")

    def test_incomplete_source_doi_is_not_invented(self):
        from core.parse.parse_manuscript import _make_reference

        for value in ("10.1016/j.example.2009.09.0", "10.1016/j.example.2017.03."):
            with self.subTest(value=value):
                ref = _make_reference(1, f"Article. https://doi.org/{value}")
                self.assertEqual(ref["doi"], value.rstrip("."))
                self.assertEqual(ref["url"], f"https://doi.org/{value.rstrip('.')}")

    def test_doi_split_mid_token_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # A wrap inside a word left a truncated DOI ("10.1093/be") that resolves
        # to nothing; the continuation carries DOI structure, so it is glued back.
        for raw, expected in (
            ("Doe J. Behav Ecol. 2013. doi:10.1093/be heco/art050",
             "10.1093/beheco/art050"),
            ("Roe R. Biol Reprod. 1971. doi:10.1093/bi olreprod/4.3.239",
             "10.1093/biolreprod/4.3.239"),
            ("Poe P. Mol Cell Endocrinol. 2020. doi:10.1016/j.mc e.2020.110949",
             "10.1016/j.mce.2020.110949"),
            ("Moe M. Obstet Gynecol. 2018. doi:10.1097/AOG.000000 0000002899",
             "10.1097/AOG.0000000000002899"),
            ("Coe C. Biol Rev. 2004. doi:10.1017/S14647 93104006529",
             "10.1017/S1464793104006529"),
        ):
            self.assertEqual(_make_reference(1, raw)["doi"], expected, raw)

    def test_prose_after_a_complete_doi_is_never_glued_on(self):
        from core.parse.parse_manuscript import _make_reference
        # Gluing a following word onto a finished DOI would forge an identifier
        # that may itself resolve — to the wrong work. A capitalised word or
        # acronym is prose picking up again, never a DOI continuation.
        for raw, expected in (
            ("Trial results. Lancet Oncol. 2023. doi:10.1016/S1470-2045(23)00156-0 JAMA "
             "commentary follows.", "10.1016/S1470-2045(23)00156-0"),
            ("Data note. EBioMedicine. 2023. doi:10.1016/j.ebiom.2023.104454 SUPPLEMENT.",
             "10.1016/j.ebiom.2023.104454"),
            ("Pain study. J Pain. 2023. doi:10.1097/AJP.0000000000001131 Xie et al. replied.",
             "10.1097/AJP.0000000000001131"),
            ("Leadership. TCR. 2023. doi:10.1177/01614681211048657. About the authors.",
             "10.1177/01614681211048657"),
        ):
            self.assertEqual(_make_reference(1, raw)["doi"], expected, raw)

    def test_valid_doi_before_prose_year_is_not_corrupted(self):
        from core.parse.parse_manuscript import _make_reference
        # A valid DOI trailed by a prose year must survive: "2013" is not a
        # DOI-suffix continuation (no internal ./-), so nothing is glued.
        ref = _make_reference(
            1, "Smith J. A great paper. Nature. doi: 10.1038/abc123. 2013 was the year.")
        self.assertEqual(ref["doi"], "10.1038/abc123")

    def test_alphanumeric_doi_before_spaced_numbers_is_not_corrupted(self):
        from core.parse.parse_manuscript import _make_reference
        # A complete DOI ending on an alphanumeric (no trailing ")./-") followed
        # by a space and hyphenated numbers must not be glued.
        ref = _make_reference(
            1, "Doe A. Paper. J Foo. doi:10.1234/abcxyz 12-34 pmid 999")
        self.assertEqual(ref["doi"], "10.1234/abcxyz")

    def test_doi_split_before_internal_period_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # A PDF wrap that fell just before a DOI-internal period leaves the head
        # unfinished ("…/j.clgc", "…/jmor", "…/rsbl", "…8") and the period alone
        # on the continuation.  Real icae138 entries: the numeric suffix after
        # the period is the rest of the DOI and must be glued back.
        cases = [
            ("Dell'Atti L, Galosi AB. 2018. Clin Genitourin Can 16:e263-7. "
             "https://doi.org/10.1016/j.clgc .2017.10.006",
             "10.1016/j.clgc.2017.10.006"),
            ("A. Morphology study. J Morphol. https://doi.org/10.1002/jmor .20272",
             "10.1002/jmor.20272"),
            ("B. Biology letter. Biol Lett. https://doi.org/10.1098/rsbl .2015.0694",
             "10.1098/rsbl.2015.0694"),
            ("C. Book chapter. https://doi.org/10.1016/B978-0-12-809633-8 .20533-5",
             "10.1016/B978-0-12-809633-8.20533-5"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_make_reference(1, raw)["doi"], expected)

    def test_wrapped_bare_year_after_doi_is_not_glued_on(self):
        from core.parse.parse_manuscript import _make_reference
        # The .-leading rule must reject a bare wrapped year: a single group
        # (".2013") carries no internal "\d[.\-]\d" and is under five digits, so
        # the complete DOI is left whole and the year drops as prose.
        ref = _make_reference(
            1, "Smith J. A paper. Nature. doi:10.1234/abcd .2013 was a good year.")
        self.assertEqual(ref["doi"], "10.1234/abcd")

    def test_doi_with_mangled_sici_glyphs_is_restored(self):
        from core.parse.parse_manuscript import _make_reference
        # PDF extraction can mangle a Wiley SICI DOI's "<"/">" into look-alike
        # "≤"/"≥"; restoring them yields a DOI doi.org can resolve (icae138 #82).
        ref = _make_reference(
            1, "Author. Anat Rec. https://doi.org/10.1002/(SICI)1097-0185"
               "(19991101)257:2≤50::AID-AR4≥3.0.CO;2-W")
        self.assertEqual(
            ref["doi"],
            "10.1002/(SICI)1097-0185(19991101)257:2<50::AID-AR4>3.0.CO;2-W")

    def test_oscola_doi_wrap_trailing_bracket_is_stripped(self):
        from core.parse.parse_manuscript import _make_reference
        # An OSCOLA "<https://doi.org/…>" wrap leaves a trailing ">" that is
        # never DOI-final and must be stripped (GroJIL #125).
        ref = _make_reference(
            1, "Author, 'A title' (2020) <https://doi.org/10.3366/gels.2020.0029> "
               "(accessed 1 January 2021).")
        self.assertEqual(ref["doi"], "10.3366/gels.2020.0029")

    def test_url_split_by_wrap_space_after_slash_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference
        # A PDF wrap split the URL after a path separator:
        # "github.com/rwightman/ pytorch-image-models" -> the extractor otherwise
        # keeps the wrong page ".../rwightman/" (the user profile, not the repo).
        ref = _make_reference(
            1, "Ross Wightman. Pytorch image models. "
               "https://github.com/rwightman/ pytorch-image-models, 2019.")
        self.assertEqual(ref["url"], "https://github.com/rwightman/pytorch-image-models")

    def test_url_split_before_slash_segment_is_rejoined(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "News article. https://example.org/trump-ukraine-postwar-world /681745")
        self.assertEqual(
            ref["url"], "https://example.org/trump-ukraine-postwar-world/681745")

    def test_url_is_not_joined_to_a_later_prose_path(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "News article. https://example.org/story See /appendix for details.")
        self.assertEqual(ref["url"], "https://example.org/story")

    def test_short_midword_url_wrap_is_rejoined_without_changing_raw_evidence(self):
        from core.parse.parse_manuscript import _make_reference

        cases = (
            (
                "News article. https://example.org/how-wo mens-rights-developed",
                "https://example.org/how-womens-rights-developed",
            ),
            (
                "Cable map. https://www.submarinecablemap.com/cou ntry/singapore",
                "https://www.submarinecablemap.com/country/singapore",
            ),
            (
                "Policy article. https://example.org/concert-of-powers-for-global-er a-by-design",
                "https://example.org/concert-of-powers-for-global-era-by-design",
            ),
        )
        for raw, expected in cases:
            with self.subTest(raw=raw):
                ref = _make_reference(1, raw)
                self.assertEqual(ref["url"], expected)
                self.assertEqual(ref["raw_entry"], raw)

    def test_midword_url_wrap_outside_short_word_gate_is_not_joined(self):
        from core.parse.parse_manuscript import _make_reference

        ref = _make_reference(
            1, "News article. https://example.org/go accessed-by-researchers")
        self.assertEqual(ref["url"], "https://example.org/go")

    def test_url_before_prose_is_not_glued(self):
        from core.parse.parse_manuscript import _make_reference
        # A URL trailed by prose ("Accessed …") must not absorb the prose word.
        ref = _make_reference(
            1, "Foo B. A page. https://example.org/docs/ Accessed May 1, 2020.")
        self.assertEqual(ref["url"], "https://example.org/docs/")












class TestInlineDoi(unittest.TestCase):
    """'Draft' citation format: the paper identifier in parentheses right after
    the statement, with no bibliography. Each identifier synthesises a reference."""

    def _parse_md(self, body):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            with open(p, "w") as f:
                f.write("# Draft\n\n" + body + "\n")
            return parse(p, 1)[0]

    def test_auto_detects_inline_doi_mode(self):
        parsed = self._parse_md(
            "The treatment reduces mortality (10.1038/s41586-022-05543-x).")
        self.assertEqual(parsed["manuscript"]["citation_mode"], "inline-doi")

    def test_bare_doi_becomes_reference_and_citation(self):
        parsed = self._parse_md(
            "The treatment reduces mortality (10.1038/s41586-022-05543-x).")
        self.assertEqual(len(parsed["references"]), 1)
        ref = parsed["references"][0]
        self.assertEqual(ref["doi"], "10.1038/s41586-022-05543-x")
        self.assertEqual(ref["source_type"], "article")
        self.assertEqual(len(parsed["claims"]), 1)
        self.assertEqual(len(parsed["citations"]), 1)
        self.assertEqual(parsed["citations"][0]["ref_id"], ref["id"])
        self.assertEqual(parsed["claims"][0]["marker_numbers"], [ref["ref_number"]])

    def test_arxiv_id_canonicalised_to_doi(self):
        parsed = self._parse_md("Transformers reshaped NLP (arXiv:1706.03762).")
        self.assertEqual(parsed["manuscript"]["citation_mode"], "inline-doi")
        self.assertEqual(parsed["references"][0]["doi"], "10.48550/arXiv.1706.03762")

    def test_doi_url_form(self):
        parsed = self._parse_md(
            "The guideline was updated (https://doi.org/10.1001/jama.2020.1585).")
        self.assertEqual(parsed["references"][0]["doi"], "10.1001/jama.2020.1585")

    def test_two_dois_in_one_paren_are_multisource(self):
        parsed = self._parse_md(
            "Two mechanisms were proposed (10.1016/j.cell.2019.01.001; "
            "10.1126/science.abc1234).")
        self.assertEqual(len(parsed["references"]), 2)
        self.assertEqual(len(parsed["claims"]), 1)
        self.assertTrue(parsed["claims"][0]["is_multisource"])
        linked = {c["ref_number"] for c in parsed["citations"]}
        self.assertEqual(len(linked), 2)

    def test_distant_dois_split_into_fragment_claims(self):
        parsed = self._parse_md(
            "Mortality dropped sharply (10.1038/aaa.111), while incidence rose "
            "steadily (10.1038/bbb.222).")
        self.assertEqual(len(parsed["claims"]), 2)
        c1, c2 = parsed["claims"]
        self.assertIn("Mortality dropped", c1["sentence"])
        self.assertNotIn("incidence", c1["sentence"])
        self.assertEqual(c1["claim_scope"], "sentence_fragment")
        by_claim = {c["claim_id"]: c["ref_number"] for c in parsed["citations"]}
        self.assertNotEqual(by_claim[c1["id"]], by_claim[c2["id"]])

    def test_repeated_doi_dedups_to_one_reference(self):
        parsed = self._parse_md(
            "First use (10.1038/aaa.111) of the method. Reused later "
            "in a second cohort (10.1038/aaa.111).")
        self.assertEqual(len(parsed["references"]), 1)
        self.assertEqual(len(parsed["claims"]), 2)
        ref_id = parsed["references"][0]["id"]
        self.assertTrue(all(c["ref_id"] == ref_id for c in parsed["citations"]))

    def test_numeric_markers_win_over_stray_prose_doi(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            with open(p, "w") as f:
                f.write(
                    "# N\n\nEffect holds [1]. Another [2]. Third [3]. Data at "
                    "10.5061/dryad.zzz for reuse.\n\n## References\n\n"
                    "[1] A. One. J. 2018.\n[2] B. Two. J. 2019.\n[3] C. Three. J. 2020.\n")
            parsed = parse(p, 1)[0]
        self.assertEqual(parsed["manuscript"]["citation_mode"], "numeric")
        self.assertEqual(len(parsed["claims"]), 3)

    def test_prose_doi_outside_parens_is_not_a_citation(self):
        parsed = self._parse_md(
            "The data live at 10.5061/dryad.zzz and were analyzed with a tool "
            "(10.1038/real.cite).")
        # only the parenthetical DOI is a citation; the bare prose DOI is a locator
        self.assertEqual(len(parsed["references"]), 1)
        self.assertEqual(parsed["references"][0]["doi"], "10.1038/real.cite")

    def test_explicit_inline_doi_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "m.md")
            with open(p, "w") as f:
                f.write("# D\n\nA claim with an identifier (10.1038/x.1).\n")
            parsed = parse(p, 1, "inline-doi")[0]
        self.assertEqual(parsed["manuscript"]["citation_mode"], "inline-doi")
        self.assertEqual(len(parsed["citations"]), 1)










# ---------------------------------------------------------------------------
# Finding 6 — bibliography end-boundary and per-reference bleed truncation
# ---------------------------------------------------------------------------













































class TestReferenceCoverage(unittest.TestCase):
    """A document that lists N references cites nearly all of them.  When it does not,
    its citation markers were lost — and nothing else we measure would say so: an orphan
    is a citation whose source is missing, so a citation that was never DETECTED cannot
    become one.  A paper that cites by superscript, handed to us as .txt, parses to no
    markers, no claims and zero orphans."""

    def _cov(self, refs, citations, source=None):
        from core.parse.parse_manuscript import _reference_coverage
        return _reference_coverage(refs, citations, source)

    def _refs(self, n):
        return [{"id": f"r{i}", "ref_number": i} for i in range(1, n + 1)]

    def _cites(self, ref_ids):
        return [{"claim_id": "c1", "ref_id": rid} for rid in ref_ids]

    def test_a_fully_cited_bibliography_does_not_warn(self):
        cov = self._cov(self._refs(10), self._cites(f"r{i}" for i in range(1, 11)))
        self.assertEqual(cov["pct"], 100.0)
        self.assertIsNone(cov["warning"])

    def test_a_few_uncited_references_do_not_warn(self):
        """Healthy parses on the corpus sit at 76-100%; the threshold is below that."""
        cov = self._cov(self._refs(40), self._cites(f"r{i}" for i in range(1, 32)))
        self.assertIsNone(cov["warning"])

    def test_markers_eaten_wholesale_warns(self):
        cov = self._cov(self._refs(13), self._cites(["r1", "r2", "r3"]))
        self.assertIsNotNone(cov["warning"])
        self.assertIn("3 of 13", cov["warning"])

    def test_a_bibliography_nobody_cites_warns(self):
        """The silent failure in its pure form: 22 references, no citations, no orphans."""
        cov = self._cov(self._refs(22), [])
        self.assertEqual(cov["pct"], 0.0)
        self.assertIsNotNone(cov["warning"])

    def test_the_warning_names_the_format_as_the_cause_when_it_is(self):
        eaten = self._cites(["r1"])
        text = self._cov(self._refs(13), eaten, source=None)["warning"]
        self.assertIn("cannot represent a superscript", text)
        glyphs = self._cov(self._refs(13), eaten, source="glyphs")["warning"]
        self.assertNotIn("cannot represent a superscript", glyphs)

    def test_a_short_bibliography_is_not_judged(self):
        """Two entries with one cited is 50% and perfectly normal — the ratio only
        means something once there are enough references for it to mean something."""
        self.assertIsNone(self._cov(self._refs(2), self._cites(["r1"]))["warning"])

    def test_no_references_at_all_does_not_warn(self):
        cov = self._cov([], [])
        self.assertEqual(cov, {"cited": 0, "total": 0, "pct": 0.0,
                               "in_prose": 0, "in_tables": 0, "uncited": [],
                               "warning": None, "fatal": False})

    def test_only_a_blind_format_is_fatal(self):
        """A format that CAN carry the markers and lost them anyway is OUR bug, not a
        reason to reject the manuscript — so it warns.  A format that cannot carry them
        has destroyed them upstream, and no re-run of ours brings them back."""
        eaten = self._cites(["r1"])
        for source in ("glyphs", "markup", "cite-commands", "text-heuristic", "unknown"):
            cov = self._cov(self._refs(13), eaten, source=source)
            self.assertIsNotNone(cov["warning"], source)
            self.assertFalse(cov["fatal"], source)
        self.assertTrue(self._cov(self._refs(13), eaten, source=None)["fatal"])

    def test_an_extractor_that_does_not_declare_is_not_treated_as_blind(self):
        """Absent is not the same as a declared None: a third-party extractor that says
        nothing gets the benefit of the doubt (a warning), not a refusal."""
        from core.parse.parse_manuscript import (_reference_coverage,
                                                 _SUPERSCRIPT_UNDECLARED)
        meta = {}
        cov = _reference_coverage(
            self._refs(13), self._cites(["r1"]),
            meta.get("superscript_source", _SUPERSCRIPT_UNDECLARED))
        self.assertFalse(cov["fatal"])




class TestCitationMarkersLostIsFatal(unittest.TestCase):
    """A superscript-cited paper supplied as .txt has no citations left to verify.
    Parsing it "successfully" would spend a whole verification run on 40% of a
    manuscript and report a clean bill of health, so the parser refuses."""

    def _write(self, d, name, text):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def _superscript_paper_as_plain_text(self):
        """What a .txt of an AMA-style paper looks like: the raised digits are gone,
        so not one marker survives — but the bibliography is still perfectly readable."""
        body = "\n".join(
            f"Finding number {i} was reported in a large cohort study." for i in range(1, 9))
        refs = "\n".join(
            f"{i}. Author A{i}. A study of things. J Test Med. 2020;{i}(1):1-10."
            for i in range(1, 11))
        return f"# Results\n\n{body}\n\nReferences\n{refs}\n"

    def test_a_superscript_paper_as_txt_is_refused(self):
        from core.parse.parse_manuscript import parse, CitationMarkersLost
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.txt", self._superscript_paper_as_plain_text())
            with self.assertRaises(CitationMarkersLost) as ctx:
                parse(p, 1, "auto")
        self.assertIn("PDF or DOCX", str(ctx.exception))

    def test_a_bracket_cited_paper_as_txt_parses_fine(self):
        """The refusal is about LOST markers, not about the format: brackets survive
        plain text, so a .txt that kept its citations is parsed as usual."""
        from core.parse.parse_manuscript import parse
        text = self._superscript_paper_as_plain_text().replace(
            "cohort study.", "cohort study [1,2,3,4,5,6,7,8,9,10].")
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.txt", text)
            parsed = parse(p, 1, "auto")[0]
        cov = parsed["_debug"]["reference_coverage"]
        self.assertEqual(cov["pct"], 100.0)
        self.assertIsNone(cov["warning"])

    def test_the_operator_can_override_and_still_sees_the_warning(self):
        from core.parse.parse_manuscript import parse
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.txt", self._superscript_paper_as_plain_text())
            old = os.environ.get("CITATION_VERIFIER_ALLOW_LOST_MARKERS")
            os.environ["CITATION_VERIFIER_ALLOW_LOST_MARKERS"] = "1"
            try:
                parsed = parse(p, 1, "auto")[0]
            finally:
                if old is None:
                    del os.environ["CITATION_VERIFIER_ALLOW_LOST_MARKERS"]
                else:
                    os.environ["CITATION_VERIFIER_ALLOW_LOST_MARKERS"] = old
        self.assertIsNotNone(parsed["_debug"]["reference_coverage"]["warning"])




class TestAFormatIsBlindOnlyWhenTheFileIs(unittest.TestCase):
    """Whether the markers survived is a fact about the FILE, not about its extension.
    The same paper, exported by a converter that kept its superscripts and by one that
    flattened them, must be accepted in the first case and refused in the second."""

    def _write(self, d, name, text):
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def _paper(self, marker):
        """An AMA-style paper: every citation is a superscript, written as *marker*."""
        body = "\n".join(
            f"Finding number {i} was reported in a large cohort study{marker(i)}"
            for i in range(1, 11))
        refs = "\n".join(
            f"{i}. Author A{i}. A study of things. J Test Med. 2020;{i}(1):1-10."
            for i in range(1, 11))
        return f"Results\n\n{body}\n\nReferences\n{refs}\n"

    def _parse(self, name, marker):
        from core.parse.parse_manuscript import parse
        with tempfile.TemporaryDirectory() as d:
            return parse(self._write(d, name, self._paper(marker)), 1, "auto")[0]

    def test_a_markdown_that_kept_its_superscripts_is_parsed(self):
        cases = {
            "html.md": lambda i: f".<sup>{i}</sup>",
            "pandoc.md": lambda i: f".^{i}^",
            "unicode.txt": lambda i: "." + "".join("⁰¹²³⁴⁵⁶⁷⁸⁹"[int(c)] for c in str(i)),
        }
        for name, marker in cases.items():
            with self.subTest(name=name):
                parsed = self._parse(name, marker)
                cov = parsed["_debug"]["reference_coverage"]
                self.assertEqual(parsed["_debug"]["extract"]["superscript_source"],
                                 "markup")
                self.assertEqual(cov["pct"], 100.0)
                self.assertFalse(cov["fatal"])

    def test_the_same_paper_flattened_is_still_refused(self):
        from core.parse.parse_manuscript import CitationMarkersLost
        with self.assertRaises(CitationMarkersLost):
            self._parse("flat.md", lambda i: f".{i}")

    def test_a_numeric_citation_link_keeps_its_brackets(self):
        # "[1](#ref-1)" is how a Markdown export links a marker to its entry.  Unwrapping
        # it to the link text alone would leave a bare "1" and throw the marker away.
        from core.parse.extractors.markdown import _md_text
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "m.md", "As shown [1](#ref-1) and [2,3](#ref-2).\n"
                                       "See [the paper](http://example.com/x).\n")
            out = _md_text(p)
        self.assertIn("[1]", out)
        self.assertIn("[2,3]", out)
        self.assertNotIn("the paper]", out)


class TestVerifyTableCitationsToggle(unittest.TestCase):
    """A table row asserts nothing, so by default its markers produce no claim — the
    reference is still counted as cited and named in the report.  The toggle exists for
    the papers whose tables ARE the argument: it puts the row back in as an ordinary
    claim, at the price of verdicts on rows that assert nothing."""

    def _numeric(self, sentences, n_refs=3):
        from core.parse.parse_manuscript import _build_numeric, _make_reference
        refs = [_make_reference(n, f"Author {n}. Title {n}. Journal. 2020;1:1-2.")
                for n in range(1, n_refs + 1)]
        return _build_numeric(sentences, refs, 1, "ms-x")

    @contextlib.contextmanager
    def _with_toggle(self, value):
        from core.parse.parse_manuscript import ENV_VERIFY_TABLE_CITATIONS as env
        old = os.environ.get(env)
        os.environ[env] = value
        try:
            yield
        finally:
            if old is None:
                os.environ.pop(env, None)
            else:
                os.environ[env] = old

    def test_default_is_drop(self):
        from core.parse import parse_manuscript
        with self._with_toggle("0"):
            self.assertFalse(parse_manuscript.verify_table_citations())
            claims, _cits, _rows, extra = self._numeric(["RESULTS [1] 91.4 [2] 91.5 [3]"])
        self.assertEqual(claims, [], "a benchmark row must produce no claim by default")
        self.assertEqual(len(extra.get("table_only_citations") or []), 3,
                         "the dropped markers must still be reported as cited")

    def test_toggle_on_makes_the_table_row_an_ordinary_claim(self):
        with self._with_toggle("1"):
            claims, cits, _rows, extra = self._numeric(["RESULTS [1] 91.4 [2] 91.5 [3]"])
        self.assertTrue(claims, "with the toggle on the row must produce a claim")
        self.assertEqual({c["ref_number"] for c in cits}, {1, 2, 3})
        self.assertEqual(extra.get("table_only_citations") or [], [],
                         "nothing is table-only once the table is verified")

    def test_toggle_promotes_ambiguous_checklist_gap_with_provenance(self):
        from core.parse.parsing_common import default_segment_sentences
        sentences = default_segment_sentences(
            "1a\n"
            "Prior evidence supports\n"
            "this revision [9]\n"
            "1b\n"
            "Recruitment setting\n"
            "2a\n"
            "Outcome assessment\n"
            "2b\n"
            "Analysis population"
        )
        with self._with_toggle("1"):
            claims, cits, _rows, extra = self._numeric(
                sentences, n_refs=10
            )
        self.assertEqual([row["ref_number"] for row in cits], [9])
        self.assertEqual(len(claims), 1)
        self.assertTrue(
            claims[0]["structural_provenance"]["structural_ambiguous"]
        )
        self.assertEqual(extra.get("table_only_citations") or [], [])

    def test_toggle_on_author_year(self):
        from core.parse.parse_manuscript import _make_reference
        from core.parse import citation_schemes
        refs = [_make_reference(1, "Smith, J. (2019). A paper. Journal, 1(1), 1-2.")]
        sent = ["Model & 91.4 & (Smith, 2019) & 88.2 & 90.1"]
        ay = citation_schemes.require_author_year()
        with self._with_toggle("1"):
            claims, cits, _rows, _extra = ay.build(sent, refs, 1, "ms-y")
        self.assertTrue(claims, "with the toggle on the author-year row is verified too")
        self.assertEqual([c["marker_raw"] for c in cits], ["(Smith, 2019)"])
        with self._with_toggle("0"):
            claims_off, _c, _r, _e = ay.build(sent, refs, 1, "ms-y")
        self.assertEqual(claims_off, [], "off by default, here as well")

    def test_the_toggle_never_changes_the_coverage_number(self):
        """Dropping a table citation from verification must not drop it from the
        record: coverage answers "is this reference cited anywhere", and the answer
        does not depend on whether we chose to verify it."""
        from core.parse.parse_manuscript import _reference_coverage, _make_reference
        refs = [_make_reference(n, f"Author {n}. T. J. 2020;1:1-2.") for n in (1, 2, 3)]
        sents = ["RESULTS [1] 91.4 [2] 91.5 [3]"]
        with self._with_toggle("0"):
            _cl, cits_off, _r, extra_off = self._numeric(sents)
        cov_off = _reference_coverage(refs, cits_off, "glyphs",
                                      table_only=extra_off.get("table_only_citations"))
        with self._with_toggle("1"):
            _cl, cits_on, _r, extra_on = self._numeric(sents)
        cov_on = _reference_coverage(refs, cits_on, "glyphs",
                                     table_only=extra_on.get("table_only_citations"))
        self.assertEqual(cov_off["pct"], 100.0)
        self.assertEqual(cov_on["pct"], cov_off["pct"])
        self.assertEqual(cov_on["uncited"], cov_off["uncited"])


