"""Static sanity checks of the MQL5 sources (no MetaEditor here, so this is a lint, not a compile).

Checks: balanced braces/parentheses outside strings and comments, every ZGB_* function
that is called is defined, the 7-argument WebRequest form, no NUL terminator sent
(StringToCharArray with StringLen count), StringFormat placeholder/argument counts,
and no secrets or personal defaults in the inputs.
"""
import re
import unittest

from helpers import REPO

MQH = REPO / "mql5" / "ZumiGitHubBridge.mqh"
DEMO = REPO / "mql5" / "ZumiGitHubBridge_Demo.mq5"


def strip_code(text: str) -> str:
    """Remove comments and the content of string / char literals."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "\"'":
            q, i = c, i + 1
            while i < n and text[i] != q:
                i += 2 if text[i] == "\\" else 1
            out.append(q + q)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_args(s: str) -> list[str]:
    """Split a call's argument string on top-level commas (strings already handled by caller)."""
    args, depth, cur, i = [], 0, "", 0
    while i < len(s):
        c = s[i]
        if c == '"':
            j = i + 1
            while s[j] != '"':
                j += 2 if s[j] == "\\" else 1
            cur += s[i:j + 1]
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    if cur.strip():
        args.append(cur)
    return args


def string_format_calls(text: str):
    for m in re.finditer(r"\b(?:StringFormat|PrintFormat)\(", text):
        i, depth = m.end(), 1
        while depth:
            c = text[i]
            if c == '"':
                i += 1
                while text[i] != '"':
                    i += 2 if text[i] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1
        yield text[m.end():i - 1]


class Mql5StaticTest(unittest.TestCase):
    def setUp(self):
        self.mqh = MQH.read_text(encoding="utf-8")
        self.demo = DEMO.read_text(encoding="utf-8")

    def test_balanced(self):
        for name, text in (("mqh", self.mqh), ("demo", self.demo)):
            code = strip_code(text)
            for o, c in ("{}", "()", "[]"):
                self.assertEqual(code.count(o), code.count(c), f"{name}: unbalanced {o}{c}")

    def test_called_functions_are_defined(self):
        code = strip_code(self.mqh) + strip_code(self.demo)
        defined = set(re.findall(r"^\s*(?:[A-Za-z_]\w*\s+)+&?(ZGB_\w+)\s*\(", code, re.M))
        called = set(re.findall(r"\b(ZGB_\w+)\s*\(", code))
        self.assertEqual(called - defined, set())

    def test_webrequest_and_char_arrays(self):
        calls = re.findall(r"WebRequest\(([^;]*)\);", self.mqh)
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(len(split_args(c)), 7, c)
        # body conversion must not include the trailing NUL
        self.assertIn("StringToCharArray(body, data, 0, len, CP_UTF8)", self.mqh)
        self.assertNotIn("WHOLE_ARRAY", strip_code(self.mqh))
        self.assertIn("CharArrayToString(result, 0, ArraySize(result), CP_UTF8)", self.mqh)
        self.assertIn("MQL_TESTER", self.mqh)
        for code in ("4014", "4060", "5202"):
            self.assertIn(code, self.mqh)

    def test_string_format_arity(self):
        for src in (self.mqh, self.demo):
            for call in string_format_calls(src):
                args = split_args(call)
                fmt = re.sub(r'"\s*\n?\s*"', "", args[0].strip())  # join adjacent literals
                n_spec = len(re.findall(r"%(?!%)[-+ #0]*\d*(?:\.\d+)?(?:I64|I32|ll|l|h)?[dioufFeEgGxXcsS]", fmt))
                self.assertEqual(n_spec, len(args) - 1, call[:120])

    def test_safe_defaults(self):
        self.assertRegex(self.mqh, r"input bool\s+InpGhEnable\s+= false;")
        self.assertRegex(self.mqh, r'input string InpGhToken\s+= "";')
        self.assertRegex(self.mqh, r"input bool\s+InpTgEnable\s+= false;")
        self.assertRegex(self.mqh, r"input bool\s+InpDcEnable\s+= false;")
        self.assertNotRegex(self.mqh + self.demo, r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_")


if __name__ == "__main__":
    unittest.main()
