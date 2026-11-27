# Reason codes / Mã lý do

Every directive in `control/control.json` carries one of these codes. Public text uses codes only (no method names, no
prices). `scripts/control.py` rejects unknown codes. Mỗi chỉ thị trong `control.json` phải có một mã lý do dưới đây.

| code | English | Tiếng Việt | typical directive |
|---|---|---|---|
| R00 | neutral / no specific reason | trung tính, không có lý do riêng | global at 1.0 |
| R01 | method losing streak | phương pháp đang thua liên tiếp | `pause` / `reduce` a method code |
| R02 | phase mismatch / weak record in this phase | phương pháp không hợp phase hiện tại (thành tích yếu trong phase này) | `pause` / `reduce` with `phases` |
| R03 | news event | sự kiện tin tức | news `block` window, or an `allow` window |
| R04 | volatility shock | biến động bất thường (VIX, vàng > k x ATR) | global `risk_mult` < 1 or `pause_all` |
| R05 | drawdown guard | bảo vệ khi drawdown | global `risk_mult` < 1 |
| R06 | data quality | dữ liệu có vấn đề | neutral or reduced risk |
| R07 | opportunity / strong phase fit | cơ hội / phương pháp rất hợp phase hiện tại | `boost` (within the EA cap) or `on`, or an `allow` window |
| R08 | method re-enable after recovery | bật lại phương pháp sau khi đã hồi phục | `on` (lifts an earlier pause, or enables an off-by-default method) |
| R09 | direction bias from multi-timeframe confluence | định hướng chiều lệnh theo đồng thuận đa khung | `global.side` buy / sell (restrict entries to one side) |

Rules (pre-registered, see `config/settings.json` > `coordinator` and `docs/control.md`):

* every directive expires (default 48 h, at most 7 days); `control.json` older than 72 h is ignored (NEUTRAL);
* `risk_mult` is in [0, 2.0] in the file, but the EA clamps it to its own input `InpGhMaxRiskMult` (default 1.0);
* the EA's max risk per trade, daily/weekly loss stops and crisis layer can never be overridden; the channel never
  opens or closes trades and never changes SL/TP.
