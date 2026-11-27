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
        called = {c for c in called if not re.fullmatch(r"ZGB_[A-Z0-9_]+", c)}   # macros like ZGB_CTL_MAX_TTL (...)
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
        self.assertRegex(self.mqh, r"input int\s+InpGhReportMode\s+= 0;")   # aggregate by default (public repo)
        self.assertIn('"kind\\":\\"ea_agg\\"', self.mqh)

    def _body(self, signature: str) -> str:
        start = self.mqh.index(signature)
        i, depth = self.mqh.index("{", start), 0
        for j in range(i, len(self.mqh)):
            depth += {"{": 1, "}": -1}.get(self.mqh[j], 0)
            if depth == 0:
                return self.mqh[start:j + 1]
        raise AssertionError(signature)

    def test_control_api_and_hard_limits(self):
        for fn in ("bool ZGB_ReadControl(", "double ZGB_RiskMult(const string method_name, const string phase_label)",
                   "bool ZGB_MethodPaused(const string method_name, const string phase_label)",
                   "bool ZGB_MethodEnabled(", "bool ZGB_ExtraNewsBlock(const datetime t_utc)",
                   "bool ZGB_NewsAllowed(const datetime t_utc)", "double ZGB_ApplyRisk("):
            self.assertIn(fn, self.mqh)
        for name, default in (("InpGhControlEnable", "false"), ("InpGhMaxRiskMult", "1.0"),
                              ("InpGhAllowEnable", "false"), ("InpGhAllowNewsOverride", "false")):
            self.assertRegex(self.mqh, rf"input \w+\s+{name}\s+= {default};")
        active = self._body("bool ZGB_CtlActive(void)")
        self.assertIn("MQL_TESTER", active)                      # tester -> NEUTRAL
        self.assertIn("ZGB_CTL_STALE_S", active)                 # > 72 h -> NEUTRAL
        self.assertIn("#define ZGB_CTL_STALE_S (72 * 3600)", self.mqh)
        self.assertIn("#define ZGB_CTL_MAX_TTL (7 * 86400)", self.mqh)
        rm = self._body("double ZGB_RiskMult(")
        self.assertIn("InpGhMaxRiskMult", rm)                    # clamped to the EA's own cap
        self.assertIn("return(1.0)", rm)                         # inactive -> neutral multiplier
        self.assertIn("MathMin(base_risk_pct * ZGB_RiskMult(method_name, phase_label), max_risk_pct)",
                      self._body("double ZGB_ApplyRisk("))      # never above the EA max risk per trade
        orc = self._body("double ZGB_OpenRiskCap(")
        self.assertIn("return(MathMin(r, local_cap));", orc)   # the remote cap never exceeds the EA's local cap
        self.assertIn("ZGB_CtlGlobalActive()", orc)              # inactive / stale -> local cap
        self.assertIn("InpGhAllowEnable", self._body("bool ZGB_MethodEnabled("))
        allowed = self._body("bool ZGB_NewsAllowed(")
        self.assertIn("InpGhAllowNewsOverride", allowed)
        self.assertIn("ZGB_ExtraNewsBlock(t_utc)", allowed)      # a block always wins over an allow
        self.assertIn("ZGB_CtlClear();", self._body("bool ZGB_ReadControl("))   # invalid / missing -> NEUTRAL
        code = strip_code(self.mqh)
        for forbidden in ("OrderSend", "PositionClose", "PositionModify", "OrderModify", "CTrade", "TRADE_ACTION"):
            self.assertNotIn(forbidden, code)                    # the bridge never trades

    def test_market_ctx_and_heartbeat(self):
        # bridge 2.4: EA market context (mkt.ctx) + hourly heartbeat report (still present in 2.5)
        self.assertRegex(self.mqh, r'#define ZGB_VERSION "2\.[4-9]"')
        self.assertIn("#define ZGB_HAS_MKTCTX", self.mqh)
        setter = self._body("void ZGB_SetMarketCtx(const string json_obj)")
        self.assertIn("ZGB_CTX_MAX", setter)                     # size limit
        self.assertIn("'{'", setter)                             # one JSON object only
        self.assertNotIn("' '", setter)                          # no spaces / free text in the allowed charset
        self.assertIn("ctx", self._body("string ZGB_MarketJson("))
        timer = self._body("void ZGB_OnTimer(void)")
        self.assertIn('ZGB_BuildReport("heartbeat", slot_len)', timer)
        hb = timer[timer.index("2.4 heartbeat") - 200:timer.index('ZGB_BuildReport("heartbeat"')]
        for cond in ("g_zgbDirtySlot < 0", "g_zgbFlagSlot < 0", "slot != g_zgbRptSlot", "ZGB_CtxFresh(slot_len)"):
            self.assertIn(cond, hb)                              # never with a pending trade / flag, once per slot
        self.assertIn("g_zgbRptSlot =", self._body("bool ZGB_BuildReport("))
        agg = self._body("bool ZGB_BuildAggReport(")
        self.assertIn("InpGhMaxPayloadBytes", agg)               # payload limit kept: rebuilt without ctx when too big
        self.assertIn("ZGB_MarketJson(false, false)", agg)

    def test_shadow_stats(self):
        # bridge 2.5: blocked-signal shadow statistics (data.shadow), reporting only
        self.assertRegex(self.mqh, r'#define ZGB_VERSION "2\.[5-9]"')
        self.assertIn("#define ZGB_HAS_SHADOW", self.mqh)
        self.assertIn("#define ZGB_SHADOW_MAX 8000", self.mqh)
        setter = self._body("void ZGB_SetShadowStats(const string json_obj)")
        self.assertIn("ZGB_SHADOW_MAX", setter)                  # size limit
        self.assertIn("'{'", setter)                             # one JSON object only
        self.assertNotIn("' '", setter)                          # no spaces / free text in the allowed charset
        self.assertIn("g_zgbShadow = json_obj;", setter)
        agg = self._body("bool ZGB_BuildAggReport(")
        self.assertIn('",\\"shadow\\":" + g_zgbShadow', agg)     # data.shadow in the aggregate report only
        self.assertIn("for(int pass = 0; pass < 3; pass++)", agg)
        self.assertIn('shd = "";', agg)                          # dropped (after mkt.ctx) when the body is too big
        self.assertNotIn("g_zgbShadow", self._body("bool ZGB_BuildDetailReport("))

    def test_one_comment_to_code_path(self):
        # 2.5: report codes (entry deal comment) and control lookups go through the SAME function, so a pause written with a
        # report code hits the EA's entries; control phases are keyed like the server's by_phase keys
        cfc = self._body("string ZGB_CodeForComment(const string cmt)")
        self.assertIn("return(ZGB_MethodCode(s));", cfc)
        self.assertIn('StringFind(s, "[")', cfc)                 # broker suffix cut
        self.assertIn("StringSubstr(s, 0, 31)", cfc)            # MT5 order comment limit
        code = strip_code(self.mqh)
        calls = [m.start() for m in re.finditer(r"\bZGB_MethodCode\(", code)]
        start = code.index("string ZGB_CodeForComment(")
        end = code.index("string ZGB_PhaseKey(")
        outside = [c for c in calls if not start <= c < end and not code.startswith("string ZGB_MethodCode(", c - 7)]
        self.assertEqual(outside, [])                            # nobody hashes a name outside ZGB_CodeForComment
        self.assertIn("ps[n].mcode = ZGB_CodeForComment(cmt);", self.mqh)
        directive = self._body("int ZGB_CtlDirective(")
        self.assertIn("ZGB_CtlFind(ZGB_CodeForComment(method_name))", directive)
        self.assertIn("ZGB_PhaseKey(phase_label)", directive)
        pk = self._body("string ZGB_PhaseKey(const string phase)")
        self.assertIn("< 24", pk)
        self.assertIn('"none"', pk)

    def test_global_side_bias(self):
        # bridge 2.6: global.side both / buy / sell / auto (= scripts/control.py SIDES), restrict only, NEUTRAL = both
        import control
        self.assertRegex(self.mqh, r'#define ZGB_VERSION "2\.[6-9]"')
        self.assertIn("#define ZGB_HAS_SIDE", self.mqh)
        apply = self._body("bool ZGB_CtlApplyText(")
        for name in control.SIDES:
            self.assertIn(f'gside_s == "{name}"', apply)
        self.assertIn('err = "global.side"; return(false);', apply)            # unknown value -> invalid file -> NEUTRAL
        self.assertIn("g_zgbCtlSide = ZGB_SIDE_BOTH;", self._body("void ZGB_CtlClear(void)"))
        mode = self._body("int ZGB_SideMode(void)")
        self.assertIn("if(!ZGB_CtlGlobalActive()) return(ZGB_SIDE_BOTH);", mode)   # inactive / stale / expired / tester -> both
        allowed = self._body("bool ZGB_SideAllowed(const int side)")
        self.assertIn("if(m == ZGB_SIDE_BOTH || m == ZGB_SIDE_AUTO) return(true);", allowed)   # auto defers to the EA's own score
        self.assertIn("return(m == ZGB_SIDE_BUY);", allowed)
        self.assertIn("return(m == ZGB_SIDE_SELL);", allowed)


    def test_phase_key_matches_server(self):
        # emulate ZGB_PhaseKey and compare with agg._phase_key on the EA labels and odd input
        import agg

        def phase_key(p: str) -> str:
            o = "".join(ch for ch in p if ch.isascii() and (ch.isalnum() or ch in "_-"))[:24]
            return o or "none"
        for label in ("SD|PB|N", "WU|IMP|H", "R|CTR|L", "", "|||", "A" * 40, "x y,z"):
            self.assertEqual(phase_key(label), agg._phase_key(label), label)

    def test_aggregate_payload_has_no_per_trade_fields(self):
        start = self.mqh.index("string ZGB_AggJson(")
        end = self.mqh.index("string ZGB_LogJson(")
        block = self.mqh[start:end]   # aggregation + aggregate report builder
        for key in ("\\\"deal\\\"", "\\\"pos\\\"", "\\\"t_in\\\"", "\\\"t_out\\\"", "\\\"in\\\"", "\\\"out\\\"",
                    "\\\"sl\\\"", "\\\"tp\\\"", "\\\"cmt\\\"", "\\\"log\\\"", "\\\"trades\\\""):
            self.assertNotIn(key, block)
        self.assertIn("ZGB_MarketJson(false)", block)
        self.assertIn("ZGB_EquityJson(false)", block)
        self.assertRegex(self.mqh, r'input string InpGhToken\s+= "";')
        self.assertRegex(self.mqh, r"input bool\s+InpTgEnable\s+= false;")
        self.assertRegex(self.mqh, r"input bool\s+InpDcEnable\s+= false;")
        self.assertNotRegex(self.mqh + self.demo, r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_")


if __name__ == "__main__":
    unittest.main()
