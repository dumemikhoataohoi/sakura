# sakura: bot dữ liệu GitHub cho Zumi XAU EA

Kho này là "bộ não dữ liệu" của EA Zumi (MT5, vàng XAUUSDm trên Exness):

* **(a)** chuẩn bị dữ liệu bên ngoài cho EA đọc: macro FRED, lịch sự kiện Mỹ (NFP/CPI/FOMC), bối cảnh giá vàng (tùy chọn);
* **(b)** nhận báo cáo lệnh và phân tích do EA gửi về, lưu thành nhật ký giao dịch;
* **(c)** tạo trang tổng kết ngày/tuần/tháng để Claude đọc và phân tích hằng ngày;
* **(d)** phát cảnh báo khi có biến động lớn hoặc rủi ro, bằng comment trên một PR "zumi-alerts" mà Claude theo dõi.

## English summary

*sakura* is a git-scraping data bot for the Zumi XAU EA. An hourly workflow (`data.yml`) pulls FRED daily series
(DGS10, DGS2, DFII10, T10YIE, DTWEXBGS, VIXCLS, DCOILWTICO, SP500), a hand-maintained US event calendar and optional
stooq gold prices, runs data-quality guards and writes a small, versioned, causal ("value as of D-1")
`data/latest.json` plus `data/history/*.csv`, committing only when something changed. The EA reads that file through the
MQL5 include `mql5/ZumiGitHubBridge.mqh` (fail-safe, short timeouts, cached) and sends trade/analysis reports back with
`repository_dispatch` (`ea_report`). `reports.yml` stores them under `reports/YYYY/MM/DD/` with a hashed account id,
upserts a deal journal, rebuilds positions (partial closes, R from the entry SL) and renders daily/weekly/monthly
analytics (win %, profit factor, expectancy, streaks, drawdown episodes, per-method/phase stats, weekday x hour heatmap,
rule breaks) plus a static dashboard. `alerts.yml` checks VIX jumps, gold moves vs ATR, EA daily loss, losing streaks,
rule breaks, upcoming NFP/CPI/FOMC and data-quality failures, and comments (Vietnamese + English) on the open PR labelled
`zumi-alerts`, which Claude subscribes to. Only `GITHUB_TOKEN` is used in workflows; the EA uses a fine-grained PAT
limited to this repository, kept in an MT5 input or a local file, never in the repo. Start with `AGENTS.md` and `docs/`.

## Kiến trúc và luồng dữ liệu

```
FRED / stooq / lịch sự kiện ──> data.yml (mỗi giờ, phút 17) ──> data/latest.json, data/history/*.csv, data/quality.json
                                                                        │
EA MT5 + ZumiGitHubBridge.mqh <── đọc latest.json (raw hoặc API) ───────┘
      │
      └─ POST /dispatches (ea_report) ──> reports.yml ──> reports/YYYY/MM/DD/*.json, reports/journal/<hash>/
                                                          reports/daily|weekly|monthly/*.md, docs/dashboard/
alerts.yml (sau mỗi lần data/reports chạy) ──> comment trên PR "zumi-alerts" ──> Claude được thông báo
```

Chi tiết: [`docs/architecture.md`](docs/architecture.md), [`docs/workflows.md`](docs/workflows.md),
[`docs/data-dictionary.md`](docs/data-dictionary.md), [`docs/mql5-bridge.md`](docs/mql5-bridge.md).

### Các file chính

| đường dẫn | nội dung |
|---|---|
| `data/latest.json` | dữ liệu gọn cho EA, schema `zumi.latest` v1, JSON đẹp + khóa sắp xếp; mỗi chuỗi FRED có `d` (ngày quan sát), `v`, `p`, `c1`, `c1p`, `c5`, `age`; `events.next` (sự kiện sắp tới, giờ UTC + `t` unix); `flags`; `gold` (tùy chọn) |
| `data/history/<ID>.csv` | lịch sử `date,value` (hoặc OHLC cho `XAUUSD.csv`) |
| `data/quality.json` | kết quả kiểm tra chất lượng (schema, dữ liệu cũ, ngoài khoảng, nhảy bất thường) |
| `data/calendar/events_us.csv` | lịch NFP/CPI/FOMC, duy trì bằng tay |
| `reports/YYYY/MM/DD/<hash>_<ts>.json` | báo cáo EA đã làm sạch (không có số tài khoản, không có số tiền) |
| `reports/journal/<hash>/deals.jsonl`, `positions.csv` | nhật ký deal (upsert theo ticket) và vị thế dựng lại |
| `reports/daily/YYYY-MM-DD.md`, `weekly/`, `monthly/` | tổng kết: số lệnh, win %, PF, kỳ vọng R, chuỗi thua, drawdown, theo phương pháp/phase, heatmap, vi phạm quy tắc, log phase |
| `docs/dashboard/index.html` + `data.json` | dashboard tĩnh (dùng được với GitHub Pages) |
| `config/settings.json` | mọi ngưỡng: chuỗi FRED, kiểm tra chất lượng, quy tắc, cảnh báo |

**Ngữ nghĩa "value as of D-1":** mỗi giá trị trong `latest.json` là quan sát cuối cùng có ngày **trước** ngày `day`
(UTC). FRED đăng hầu hết chuỗi ngày vào ngày làm việc kế tiếp (lợi suất H.15 khoảng 16:15 giờ New York), nên giá trị
mới nhất thường là D-1 hoặc D-2; xem `age`. `DTWEXBGS` (USD) chỉ cập nhật hằng tuần.

**Lưu ý nghiên cứu:** nghiên cứu macro1 không tìm thấy lợi thế ngoài mẫu từ các chuỗi macro này cho lệnh vàng 1R.
Hãy dùng chúng như bối cảnh và bộ lọc rủi ro (chặn giờ tin, chế độ VIX), không phải tín hiệu vào lệnh.

## Cài đặt

1. **Chọn public hay private** (xem mục bên dưới), rồi push kho này lên GitHub (`main`).
2. **Actions:** bật Actions. Vào *Settings > Actions > General > Workflow permissions* và bật
   **"Allow GitHub Actions to create and approve pull requests"** để `alerts.yml` tự tạo PR cảnh báo
   (hoặc tạo PR bằng tay, xem `docs/workflows.md`). Các workflow tự khai báo `permissions` tối thiểu.
3. **Chạy thử dữ liệu:** *Actions > data > Run workflow*. Sau vài chục giây `data/latest.json` được cập nhật.
4. **Tạo PAT fine-grained cho EA** (chỉ cần nếu EA gửi báo cáo, hoặc repo private):
   *GitHub > Settings > Developer settings > Personal access tokens > Fine-grained tokens > Generate new token*
   * Repository access: **Only select repositories** > chỉ chọn repo này;
   * Repository permissions: **Contents: Read and write** (bắt buộc cho `/dispatches`; Metadata: read tự có);
   * đặt hạn dùng (ví dụ 90 ngày) và nhớ xoay vòng.
5. **MT5:** *Tools > Options > Expert Advisors > "Allow WebRequest for listed URL"*, thêm
   `https://raw.githubusercontent.com` và `https://api.github.com`
   (thêm `https://api.telegram.org` / `https://discord.com` nếu bật kênh chat).
6. **EA:** chép `mql5/ZumiGitHubBridge.mqh` vào thư mục EA, `#include` nó và gọi các hàm như trong
   `mql5/ZumiGitHubBridge_Demo.mq5`. Đặt `InpGhEnable=true`, `InpGhOwner`/`InpGhRepo`, một `InpGhAccountSalt` riêng
   (giữ bí mật), và token: input `InpGhToken` hoặc dòng đầu của file `MQL5\Files\zumi_gh_token.txt`.
7. **Kiểm tra báo cáo:** *Actions > reports > Run workflow* với payload mẫu trong `docs/workflows.md`, hoặc để EA chạy
   và chờ báo cáo cuối ngày. *Actions > alerts > Run workflow* với `dry_run` để xem cảnh báo.
8. **Claude:** đăng ký theo dõi PR có nhãn `zumi-alerts`; mỗi comment là một thông báo.

Chạy và kiểm thử trên máy:

```bash
pip install -r requirements.txt
python -m unittest discover -s tests -v      # test offline, dùng fixture
python scripts/build_latest.py               # tải FRED thật, ghi data/
python scripts/alerts.py --dry-run           # in ra các cảnh báo
```

## Bảo mật

* **Không bao giờ commit token** (PAT, token bot Telegram, URL webhook Discord). `.gitignore` chặn các file token phổ
  biến, script che (`***`) mọi chuỗi giống token trong báo cáo, log và commit message.
* Dùng **PAT fine-grained chỉ cho đúng repo này**, quyền `Contents: Read and write`, có hạn dùng. Lưu trong input của
  MT5 (file `.set`/profile trên máy bạn) hoặc file cục bộ `MQL5\Files\zumi_gh_token.txt`, **không bao giờ trong repo**.
  Lưu ý: quyền này cũng cho phép ghi nội dung repo, nên nếu lộ token phải thu hồi ngay.
* Workflow chỉ dùng `GITHUB_TOKEN` với quyền tối thiểu (`contents: write`; thêm `pull-requests`/`issues: write` cho
  alerts). `client_payload` là dữ liệu không tin cậy: chỉ đọc từ file sự kiện bằng Python, không chèn `${{ }}` vào shell.
* **Quyền riêng tư:** tài khoản chỉ xuất hiện dưới dạng hash SHA-256 có salt (tính ngay trong EA, không gửi số tài khoản);
  tiền chỉ ở dạng % số dư và R; server còn loại bỏ các khóa như `balance`, `equity`, `profit`, `login`, `email`.
  Đổi `InpGhAccountSalt` khỏi giá trị mặc định, nếu không hash của số tài khoản 8 chữ số có thể bị dò ngược.

## Public hay private?

* `raw.githubusercontent.com` chỉ đọc được **không cần token khi repo public**. Khi đó EA đọc `latest.json` không cần
  token (token chỉ để gửi báo cáo). Mọi thứ trong repo, kể cả báo cáo (đã ẩn danh, chỉ %), sẽ công khai.
* Nếu repo **private**, EA cần PAT và phải đọc qua API contents:
  `GET https://api.github.com/repos/<owner>/<repo>/contents/data/latest.json?ref=main` với
  `Accept: application/vnd.github.raw+json` (đặt `InpGhPrivateRepo=true`). GitHub Pages cho repo private cần gói trả phí.

## Cấu hình ngưỡng

`config/settings.json`: `alerts` (VIX tăng >= 15 % hoặc >= 3 điểm, VIX >= 30, lợi suất thực >= 12 bp, USD >= 1 %,
vàng >= 1,5 x ATR14, EA lỗ ngày <= -3 %, chuỗi thua >= 4, sự kiện trong 24 giờ, dữ liệu cũ > 48 giờ),
`rules` (giới hạn lỗ ngày 3 %/tuần 6 %, rủi ro mỗi lệnh 1 %, chặn tin ±15 phút), `quality` (khoảng hợp lệ,
ngưỡng nhảy bất thường, tuổi tối đa).

## Duy trì lịch sự kiện

Không có API lịch miễn phí đáng tin, nên `data/calendar/events_us.csv` được sửa bằng tay: mỗi tháng 12 (hoặc khi
BLS/Fed đổi lịch) thêm các dòng của năm sau từ lịch chính thức của BLS (Employment Situation, CPI) và lịch họp FOMC
của Fed; đánh dấu `uncertain` cho tới khi xác nhận. File ban đầu được lập từ trí nhớ (không tải về); 27 dòng
(giai đoạn đóng cửa chính phủ 2025 và năm 2026) đang có cờ `uncertain` và cần kiểm tra. Hướng dẫn chi tiết trong
`docs/data-dictionary.md`.

## Giới hạn hiện tại

* Module MQL5 mới được rà soát tĩnh (có test lint), **chưa biên dịch bằng MetaEditor**.
* Nguồn vàng stooq chưa kiểm chứng được từ môi trường build (bị chặn); khối `gold` là tùy chọn và tự bỏ qua khi lỗi.
* CFTC COT không dùng (bị Cloudflare chặn).

## Tham khảo / References

Các mẫu thiết kế được học từ những dự án sau (liên kết theo nghiên cứu của người dùng, chưa mở được từ môi trường build):

* Git scraping: Simon Willison, ["Git scraping"](https://simonwillison.net/2020/Oct/9/git-scraping/); GitHub OCTO
  [githubocto/flat](https://github.com/githubocto/flat); "Banxico al Día";
  [space-lumps/macro-financial-indicators](https://github.com/space-lumps/macro-financial-indicators);
  [DeanFinancials/deanfi-collectors](https://github.com/DeanFinancials/deanfi-collectors).
  Đã áp dụng: cron lệch phút (`17 * * * *`), commit chỉ khi đổi với message tóm tắt thay đổi, JSON đẹp và sắp xếp khóa,
  tải lại cửa sổ 60 ngày để bắt số liệu sửa đổi, kiểm tra chất lượng (schema/cũ/khoảng/nhảy) không ghi đè khi lỗi mà
  mở cảnh báo, concurrency group + `pull --rebase` + thử lại, thư mục `docs/` và `AGENTS.md`.
* MQL5 kết nối ngoài: [thomaspraun/mql-developer](https://github.com/thomaspraun/mql-developer) (ghi chú external
  communication), TradersPost, mql5-discord-telegram-logger. Đã áp dụng: `StringToCharArray(..., StringLen(body), CP_UTF8)`
  không gửi ký tự `\0`, giải mã `CP_UTF8`, xử lý rõ lỗi 4014/4060, bỏ qua trong Strategy Tester, timeout ngắn (<= 5 s),
  gọi từ `OnTimer`, hàng đợi file, kích hoạt từ `OnTradeTransaction` (DEAL_ADD) và gom lô, kênh Telegram/Discord tùy chọn
  (mặc định tắt).
* Nhật ký giao dịch: TradingJournal, aether, [LuxAlgo/trade-journal](https://github.com/LuxAlgo/trade-journal),
  [pengfuchao/trading-record-analysis](https://github.com/pengfuchao/trading-record-analysis), HxTradeHelper.
  Đã áp dụng: dựng vị thế từ deal theo `position_id` (kể cả đóng từng phần, giá thoát bình quân theo khối lượng), upsert
  idempotent, win %/PF/kỳ vọng/R theo SL lúc vào lệnh, chuỗi thua, giai đoạn drawdown, thống kê theo phương pháp/phase,
  heatmap thứ x giờ, bộ quy tắc (giới hạn lỗ ngày/tuần, lệnh không SL, rủi ro vượt mức, vào lệnh trong giờ tin), MAE/MFE
  tùy chọn, dashboard tĩnh.
* Quyền riêng tư và bảo mật theo các dự án trên: hash tài khoản, tiền theo % số dư, không commit bí mật, che bí mật
  trong log và commit message.
