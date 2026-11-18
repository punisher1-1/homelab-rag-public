import contextlib
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).with_name("parse_check.py")
SPEC = importlib.util.spec_from_file_location("parse_check", MODULE_PATH)
parse_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(parse_check)


def doc(*texts):
    return {
        "schema_name": "DoclingDocument",
        "origin": {"filename": "source.pdf"},
        "texts": [
            {
                "label": "text",
                "text": text,
                "prov": [{"page_no": index}],
            }
            for index, text in enumerate(texts, start=1)
        ],
    }


def labeled_doc(*items):
    return {
        "schema_name": "DoclingDocument",
        "origin": {"filename": "source.pdf"},
        "texts": [
            {"label": label, "text": text, "prov": [{"page_no": index}]}
            for index, (label, text) in enumerate(items, start=1)
        ],
    }


class ParseCheckTests(unittest.TestCase):
    def write_doc(self, folder, name, payload):
        path = pathlib.Path(folder) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "pass.json", doc(
                "This is a clean paragraph with ordinary readable text."
            ))
            self.assertEqual(parse_check.assess_file(path)["status"], "PASS")

    def test_warn_for_one_isolated_odd_glyph(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "warn.json", doc("图SE"))
            result = parse_check.assess_file(path)
            self.assertEqual(result["status"], "WARN")
            self.assertEqual(result["by_signal"], {"odd-letter": 1})

    def test_fail_for_encoding_damage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "fail.json", doc(
                "The agency accepts no liabilitv or responsibilitv for this."
            ))
            self.assertEqual(parse_check.assess_file(path)["status"], "FAIL")

    def test_verified_revenue_and_taxation_code_is_not_encoding_damage(self):
        self.assertEqual(parse_check.encoding_hits("Revenue and Taxation Code (R&TC).", []), [])
        self.assertEqual(parse_check.encoding_hits("The R&TC applies; select &inish.", []), ["&inish"])
        self.assertEqual(parse_check.encoding_hits("Not an exemption: R&TCbroken or &TC.", []),
                         ["&TCbroken", "&TC"])

    def test_urls_and_attack_framework_are_not_encoding_damage(self):
        for text in (
            "https://m365.cloud.microsoft &https://copilot.cloud.microsoft Microsoft Copilot Chat",
            "id=kb _ article&sys _ id=1A2bcd3e45f6ghij17890kl1mn2345678o",
            "https://instancedomain.service-now.com/sp?id=kb _ article&sys _ id=${SysId}",
            "https://instancedomain.service-now.com/esc?id=kb _ article _ view&sysparm _ article=${Number}",
            "MITRE|ATT&CK",
            "such as The MITRE ATT&CK framework https://attack.mitre.org and NIST",
        ):
            self.assertEqual(parse_check.encoding_hits(text, []), [], text)
        # The exemptions stay narrow: damaged words are still caught.
        self.assertEqual(parse_check.encoding_hits("Click &inish _ now and &ield.", []),
                         ["&inish", "&ield"])
        self.assertEqual(parse_check.encoding_hits("ATT&CKS or &CK", []), ["&CKS", "&CK"])

    def test_verified_pmbok_artifacts_still_require_human_review(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "pmbok.json", doc(
                "G L O B A L  S T A N D A R D",
                "Jun        Jul        Aug        Sep        Oct        Nov",
                "Şafak Bilgi Akdemir contributed to this publication.",
                "Mario Špundak contributed to this publication.",
                "Retrieved from https://example.org/report.pdf?v=123&sc_lang_temp=en",
            ))
            result = parse_check.assess_file(path)
            self.assertEqual(result["status"], "WARN")
            self.assertEqual(result["by_signal"], {
                "chart-axis-spacing": 1, "display-tracking": 1,
            })

    def test_unverified_tracking_and_real_scramble_still_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "damaged.json", doc(
                "S S O  S D E R",
                "Nod  g      s  :ads Designer.",
                "The source says &inish, not Finish.",
                "s s o sdə so e",
            ))
            result = parse_check.assess_file(path)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("fragments", result["by_signal"])
            self.assertIn("glyph-gaps", result["by_signal"])
            self.assertIn("encoding", result["by_signal"])
            self.assertIn("odd-letter", result["by_signal"])

    def test_months_with_other_text_are_not_excused_as_chart_axis(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "damaged-axis.json", doc(
                "Jun        Jul        Aug        s      c  corrupted body"
            ))
            result = parse_check.assess_file(path)
            self.assertEqual(result["status"], "FAIL")
            self.assertIn("glyph-gaps", result["by_signal"])

    def test_absolute_file_argument(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.write_doc(folder, "absolute.json", doc("Clean text here."))
            self.assertEqual(parse_check.expand_args([str(path)]), [path])



class CodeRepairTests(unittest.TestCase):
    """Real snippets from the approved IN254 Tkinter book and pve-admin-guide."""

    def assertRepairs(self, damaged, expected):
        repaired, hits = parse_check.repair_code_text(damaged)
        self.assertEqual(repaired, expected)
        self.assertTrue(hits)

    def test_init_definitions_and_super_calls(self):
        self.assertRepairs(
            "import tkinter as tk class App(tk.Tk): def init__(self): super().__init_()",
            "import tkinter as tk class App(tk.Tk): def __init__(self): super().__init__()")
        self.assertRepairs("def _init__(self): super().init_()",
                           "def __init__(self): super().__init__()")
        self.assertRepairs("def init_(self): super()._init_()",
                           "def __init__(self): super().__init__()")
        self.assertRepairs("def _init__(self) : super( ).init_() label_a = tk",
                           "def __init__(self) : super( ).__init__() label_a = tk")
        self.assertRepairs("items=[]): super()._init_(master)",
                           "items=[]): super().__init__(master)")

    def test_main_guard_variants(self):
        canonical = 'if __name__ == "__main__":'
        for damaged in (
            'if _name_ __ == "__main___":',
            'if _name_ II __main__":',
            'if __name___ == "__main___":',
            'if __name_ == "__main__":',
            'if _name__ == "__main__":',
            'if __name__ == __main___":',
            'if _name_ == "__main_":',
            'if __name_ "__main___":',
            'if name_ II _main__":',
            'if _name == II __main___":',
            'if __name__== "_main__":',
        ):
            with self.subTest(damaged=damaged):
                self.assertRepairs(damaged + " app.mainloop()", canonical + " app.mainloop()")
        self.assertRepairs("if _name_ '__main__':", "if __name__ == '__main__':")

    def test_rotated_app_line(self):
        self.assertRepairs('if __name__ == "__main__": ( ) dd\u2200 = dde app.mainloop()',
                           'if __name__ == "__main__": app = App() app.mainloop()')

    def test_correct_and_ambiguous_code_is_unchanged(self):
        for text in (
            'class App(tk.Tk): def __init__(self): super().__init__() '
            'if __name__ == "__main__": app = App()',
            "if __name__ == '__main__': main()",
            'if __name__=="__main__": main()',
            "self.scheduled_id = self.after(5000, self.init_buttons) def init_buttons(self):",
            "self._name = name; self.__private = 1; init_ = 0",
            "class Color(Enum): RED = 1  # Color.RED._name_ == 'RED'",
            'if name == "main": print(name)',
            "super()._init() label_a = tk.Label(self)",   # may be a real private method
            "print(threading.main_thread().name)",
            "self._last_name = last_name; self._first_name = first_name",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_check.repair_code_text(text), (text, []))
                self.assertEqual(parse_check.dunder_damage(text), [])

    def test_caret_lookalike_is_mapped_in_every_label(self):
        for label, damaged, expected in (
            ("section_header", "serial[n] : (/dev/[\u02c6,]+|socket)",
             "serial[n] : (/dev/[^,]+|socket)"),
            ("list_item", "match-field regex:hostname=\u02c6.+\\.example\\.com$",
             "match-field regex:hostname=^.+\\.example\\.com$"),
            ("text", "--snapshot (?\u02c6i:[a-z0-9_\\-]{1,40})",
             "--snapshot (?^i:[a-z0-9_\\-]{1,40})"),
            ("code", "--name (?\u02c6:\u02c6[\u02c6:/\\s]+$)", "--name (?^:^[^:/\\s]+$)"),
        ):
            with self.subTest(label=label):
                self.assertEqual(parse_check.repair_item_text(damaged, label), expected)

    def test_microsoft_learn_icon_chrome_is_skipped(self):
        # troubleshoot-mem-intune.json: icon-font glyphs in front of UI labels.
        for text in ("\uff89 Expand table", "\uff43 HOW-TO GUIDE",
                     "\uff45 OVERVIEW", "\uff49 REFERENCE"):
            with self.subTest(text=text):
                self.assertTrue(parse_check.is_web_chrome(text))
                self.assertIsNone(parse_check.inspect_item(
                    {"label": "section_header", "text": text}))
        # No icon, real content, or a non-chrome label: not chrome.
        for text in ("Overview", "REFERENCE", "Expand table",
                     "\uff89 Expand table rows", "\u5927Troubleshooting + support",
                     "\u65e5Save", "\u65e5eng_data"):
            with self.subTest(text=text):
                self.assertFalse(parse_check.is_web_chrome(text))
        self.assertIsNotNone(parse_check.inspect_item(
            {"label": "text", "text": "\u65e5eng_data"}))

    def test_windows_client_false_positives_pass(self):
        # troubleshoot-windows-client (pages 1-1500): correct text flagged in
        # three repair rounds in a row.
        for label, text in (
            ("text", "ATI Mobility Radeon HD 4650 (PCI\\VEN_1002&DEV_9480)"),
            ("text", "ATI Mobility Radeon HD 4870 (PCI\\VEN _ 1002&DEV _ 945A)"),
            ("text", '"VEN _ 8086&DEV _ 7A98" , "VEN _ 8086&DEV _ 7A99"'),
            ("text", "USB\\VID_045E&PID_07A5&MI_00"),
            ("code", "oMyShortcut.WindowStyle = 3 &&Maximized 7=Minimized 4=Normal"),
            ("text", "Error message in Groove Music, or Movies &TV when you wake"),
            ("code", "Bcdboot D:\\windows /s R: /f ALL"),
            ("text", "71 7e 26 2f 8e 03 d2 01"),
            ("section_header", "\uff35 Caution"),
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_check.inspect_item({"label": label, "text": text}))

    def test_windows_exemptions_keep_real_damage(self):
        for text, signal in (
            ("Click &inish to close the installer.", "encoding"),
            ("&DOd district office", "encoding"),
            ("s s o  sdq  so e x k", "fragments"),
            ("a b c d e f g", "fragments"),
        ):
            with self.subTest(text=text):
                hit = parse_check.inspect_item({"label": "text", "text": text})
                self.assertIsNotNone(hit)
                self.assertTrue(any(r.startswith(signal) for r in hit.reasons))

    def test_dunder_repair_is_limited_to_code_items(self):
        prose = "The modification to the _init__ method is similar."
        self.assertEqual(parse_check.repair_item_text(prose, "text"), prose)
        self.assertEqual(parse_check.repair_item_text("def init__(self):", "code"),
                         "def __init__(self):")


class DunderReviewTests(unittest.TestCase):
    def assess(self, payload):
        with tempfile.TemporaryDirectory() as folder:
            path = pathlib.Path(folder) / "book.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return parse_check.assess_file(path)

    def test_damaged_dunders_in_code_fail_review(self):
        result = self.assess(labeled_doc(
            ("text", "Let's create our first Tkinter application."),
            ("code", "class App(tk.Tk): def init__(self): super().__init_()"),
        ))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["by_signal"], {"dunder": 1})
        self.assertEqual(result["findings"][0]["reasons"], ["dunder:def init__,.__init_"])

    def test_correct_code_and_prose_mentions_pass(self):
        result = self.assess(labeled_doc(
            ("code", 'class App(tk.Tk): def __init__(self): super().__init__() '
                     'if __name__ == "__main__": app = App() app.mainloop()'),
            ("text", "The modification to the _init__ method is similar."),
        ))
        self.assertEqual(result["status"], "PASS")

    def test_encoding_and_glyph_rules_stay_active_for_code_items(self):
        result = self.assess(labeled_doc(
            ("code", "# The agency accepts no liabilitv for this."),
            ("code", "Nod  g      s  :ads Designer."),
        ))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("encoding", result["by_signal"])
        self.assertIn("glyph-gaps", result["by_signal"])

    def test_glyph_id_cipher_code_fails_review(self):
        # Docling 2.140 on IN254 (FreeMono, broken ToUnicode map), p.19/p.23.
        result = self.assess(labeled_doc(
            ("text", "Let's create our first Tkinter application."),
            ("code", "GSPN ULJOUFS JNQPSU � SPPU � 5L�� CUO � "
                     "#VUUPO�SPPU� UFYU��$MJDL NF��� "
                     "SPPU�NBJOMPPQ��"),
            ("code", "JNQPSU ULJOUFS BT UL DMBTT \"QQ�UL�5L�� EFG "
                     "@@JOJU@@�TFMG�� TVQFS���@@JOJU@@��"),
        ))
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["by_signal"], {"code-cipher": 4})
        reasons = result["findings"][1]["reasons"]
        self.assertTrue(any(r.startswith("code-cipher:shifted-glyphs") for r in reasons))
        self.assertTrue(any(r.startswith("code-cipher:replacement-chars") for r in reasons))

    def test_shifted_capitals_without_replacement_chars_fail(self):
        self.assertEqual(
            parse_check.code_cipher("JNQPSU ULJOUFS BT UL DMBTT BQQ EFG TFMG"),
            ["shifted-glyphs:7/8"],
        )

    def test_real_code_is_not_cipher(self):
        for text in (
            'from tkinter import * root = Tk() btn = Button(root, text="Click me!")',
            "import tkinter as tk RELIEFS = [tk.SUNKEN, tk.RAISED, tk.GROOVE, "
            "tk.RIDGE, tk.FLAT]",
            "SELECT NAME, EMAIL FROM USERS WHERE ID = 1 ORDER BY NAME",
            "MAX_SIZE = 10 DEFAULT_TIMEOUT = 30 API_URL = BASE + PATH",
            "Python 3.6.4 (v3.6.4:d48eceb) >>> from tkinter import >>> root = Tk()",
            "print('�') # one stray replacement char",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_check.code_cipher(text), [])

    def test_cipher_rule_is_limited_to_code_items(self):
        result = self.assess(labeled_doc(("text", "JNQPSU ULJOUFS BT UL DMBTT BQQ")))
        self.assertNotIn("code-cipher", result["by_signal"])

    def test_california_scramble_still_fails(self):
        result = self.assess(labeled_doc(("text", "Yman n n n t t n s it d s tdn)")))
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("fragments", result["by_signal"])


class BundledDictionaryTests(unittest.TestCase):
    """The bundled list must be the whole dictionary on every host.

    The rag-reviewer container has no /usr/share/dict/words. With the old
    491-word list it passed 'specifv' that the Mac (system dictionary)
    flagged: troubleshoot-windows-client (pages 1-1500) p. 1495.
    """

    def load_quietly(self, bundled, system_paths):
        err = io.StringIO()
        with mock.patch.object(parse_check, "_DICT_PATHS", system_paths), \
                contextlib.redirect_stderr(err):
            words = parse_check.load_words(bundled)
        return words, err.getvalue()

    def write_list(self, folder, name, words):
        path = pathlib.Path(folder) / name
        path.write_text("\n".join(words) + "\n", encoding="utf-8")
        return str(path)

    def test_bundled_list_is_full_size(self):
        self.assertGreaterEqual(len(parse_check.WORDS), parse_check.MIN_BUNDLED_WORDS)
        for word in ("specify", "liability", "responsibility", "widget", "you"):
            self.assertIn(word, parse_check.WORDS)
        for damaged in ("specifv", "liabilitv", "responsibilitv"):
            self.assertNotIn(damaged, parse_check.WORDS)

    def test_full_bundled_list_ignores_system_dictionary(self):
        with tempfile.TemporaryDirectory() as folder:
            system = self.write_list(folder, "words", ["specifv", "zzyzx"] * 600
                                     + [f"word{chr(97 + i % 26)}" for i in range(2000)])
            words, err = self.load_quietly(parse_check._BUNDLED_DICT, (system,))
        self.assertEqual(err, "")
        self.assertEqual(words, parse_check.WORDS)
        self.assertNotIn("specifv", words)

    def test_small_bundled_list_warns_and_falls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            bundled = self.write_list(folder, "bundled.txt", ["specify", "liability"])
            words, err = self.load_quietly(bundled, ())
            self.assertEqual(words, {"specify", "liability"})
            self.assertIn("holds only 2 words", err)

            system = self.write_list(folder, "words",
                                     ["".join(chr(97 + (i // 26 ** k) % 26) for k in range(3))
                                      for i in range(1500)])
            words, err = self.load_quietly(bundled, (system,))
            self.assertIn("specify", words)
            self.assertGreater(len(words), 1000)
            self.assertIn("holds only 2 words", err)

    def test_missing_bundled_list_warns(self):
        with tempfile.TemporaryDirectory() as folder:
            words, err = self.load_quietly(str(pathlib.Path(folder) / "none.txt"), ())
        self.assertEqual(words, set())
        self.assertIn("is missing", err)
        self.assertIn("encoding check", err)

    def test_windows_client_v_for_y_is_flagged_without_system_dictionary(self):
        # Store-measured: server PASS, Mac FAIL before; FAIL on both now.
        item = {"label": "text", "prov": [{"page_no": 1495}],
                "text": "file vou specifv (limited bv free disk space). "
                        "Which do vou prefer?"}
        finding = parse_check.inspect_item(item)
        self.assertIsNotNone(finding)
        self.assertEqual(finding.reasons, ["encoding:specifv"])

    def test_cookbook_cipher_needs_modern_words(self):
        # 'widget.bind(sequence, callback, add)' shifted. Neither the old
        # bundled list nor macOS web2 knows 'widget' or 'callback'.
        self.assertEqual(parse_check.code_cipher("XJEHFU CJOE TFRVFODF  DBMMCBDL  BEE"),
                         ["shifted-glyphs:5/5"])


if __name__ == "__main__":
    unittest.main()
