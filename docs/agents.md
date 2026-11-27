# Hệ đa agent: EA – GitHub – Claude

Yêu cầu E4, ngày 2026-11-27: xây EA như một AI agent, GitHub như một AI agent, và Claude cũng là một AI agent. Mỗi agent có chức năng riêng và có nhiều agent con. Các agent điều phối lẫn nhau theo một mục tiêu chung: **mỗi ngày đều có lệnh và có lãi**.

Tài liệu này mô tả vai trò của từng agent, hợp đồng dữ liệu giữa các agent, vòng lặp điều phối và KPI. Các giới hạn cứng không thay đổi:
- rủi ro tối đa 1%/lệnh;
- không gấp thếp, không lưới, không nhồi lệnh, không hedge;
- SL/TP đặt ngay khi vào lệnh và không ai sửa;
- không giao dịch từ xa: chỉ EA được mở lệnh và đóng lệnh;
- mất điều phối thì EA về NEUTRAL;
- repo công khai chỉ chứa số liệu gộp.

## 1. Ba agent chính

| Agent | Chạy ở đâu | Quyền | Không được làm |
|---|---|---|---|
| **EA agent** (ZumiXAU EA161) | MT5 trên máy hoặc VPS của người dùng | Quyết định từng lệnh: vào, khối lượng, SL/TP, thoát theo luật cố định | Vượt giới hạn cứng; nhận lệnh mở hoặc đóng từ bên ngoài |
| **GitHub agent** (repo sakura + Actions) | GitHub Actions, chạy theo lịch và theo sự kiện | Thu dữ liệu, lưu báo cáo gộp, tính thống kê, đề xuất `control.json`, kiểm tra hợp lệ, cảnh báo | Lưu dữ liệu từng lệnh, giá, số tài khoản; nới giới hạn của EA |
| **Claude agent** (phiên Claude Code) | 3 lần/ngày trước phiên Á, Âu, Mỹ; thêm 1 lần nghiên cứu mỗi tuần | Phân tích thị trường, quyết định điều phối (ghi `control.json`), nghiên cứu, nâng cấp EA, báo cáo cho người dùng | Triển khai lên tài khoản thật khi chưa có quyết định của người dùng; nới giới hạn cứng |

## 2. Agent con

### 2.1 EA agent

| Agent con | Chức năng | Có sẵn (bản) |
|---|---|---|
| **Market** | Xác định xu hướng đa khung với khung định hướng khớp khung vào lệnh (M1→M15/H1, M15→H4, H4→D1). Xác định trạng thái tăng / giảm / đi ngang / phá vỡ, vùng hỗ trợ/kháng cự, mô hình đảo chiều. Xuất `mkt` và `mkt.ctx` | v1.30+ (CDS, mkt.ctx 2.4) |
| **Signal** | Mỗi họ lệnh là một agent con: S4, HTX 1R, TD H4/H6 (v1.40), và các họ dự bị (TD H8/H12, T15...) đang chạy lệnh ảo | S4, HTX: v1.37; TD: v1.40 (chưa dựng) |
| **Risk** | Thang rủi ro theo chuỗi thua, ngân sách rủi ro cùng chiều, dừng lỗ ngày có tính cả lệnh đang mở (W1b), lớp khủng hoảng, bộ dừng sụt 15%, trần 1%/lệnh | v1.37 rev2 (+ W1b ở v1.40) |
| **Execution** | Gửi lệnh, xử lý phản hồi của sàn, chống lệnh trùng khi khởi động lại, kiểm tra spread, trượt giá, lot tối thiểu, ký quỹ | v1.35+ |
| **Shadow / Learning** | Ghi lệnh ảo cho mọi tín hiệu bị chặn, kèm lý do: CELL, CTRL, NEWS, CRISIS, CAP, SPREAD, BREAKER, SIDE, OTHER | cầu nối 2.5+ |
| **Reporter** | Gửi báo cáo gộp v2, heartbeat mỗi giờ và thống kê lệnh ảo lên GitHub | cầu nối 2.6 |
| **Control client** | Đọc `control.json` và kiểm tra hợp lệ. Áp dụng ở mức min(giá trị cục bộ, giá trị điều phối). Hết hạn hoặc lỗi thì về NEUTRAL | cầu nối 2.6 |

### 2.2 GitHub agent

| Agent con | Chức năng | Thành phần |
|---|---|---|
| **Data** | Lấy VIX, lợi suất thực, USD, lịch tin (FRED, lịch FF, lịch thủ công) | `data.yml`, `fred.py`, `ff_calendar.py`, `calendar_events.py`, `build_latest.py` |
| **Quality** | Kiểm tra dữ liệu cũ hoặc thiếu | `quality.py` → `data/quality.json` |
| **Report store** | Nhận và lưu báo cáo gộp của EA, chỉ giữ các trường trong whitelist | `reports.yml`, `store_report.py`, `agg.py` |
| **Analytics** | Thống kê ngày, tuần, tháng theo mã M### và giai đoạn; thống kê lệnh ảo | `daily_summary.py`, `journal_stats.py`, `reports_lib.py` |
| **Coordinator** | Áp luật điều phối lên thống kê và đề xuất `control.json` | `coordinator_report.py` (v2 đang làm), `coord_replay.py` |
| **Guard** | Chuẩn hóa `control.json`, kiểm tra hợp lệ, ghi nhật ký thay đổi | `control.py`, `control.yml` |
| **Alerts** | Đăng cảnh báo lên PR `zumi-alerts` | `alerts.yml`, `alerts.py` |

### 2.3 Claude agent

| Agent con | Chức năng |
|---|---|
| **Analyst** | Trước mỗi phiên: phân tích đa khung, mô hình đảo chiều, hỗ trợ/kháng cự, tin tức. Đưa ra 3 kịch bản (tiếp diễn / hồi rồi tiếp diễn / đảo chiều) và ghi vào sổ kịch bản để chấm điểm sau |
| **Coordinator** | Đọc báo cáo, chạy `coordinator_report.py`, quyết định và ghi `control.json` (seq, hạn, mã lý do), làm mới ít nhất mỗi 48 giờ |
| **Researcher** | Nghiên cứu theo `docs/research-protocol.md`: đăng ký trước, walk-forward, kiểm tra cuối. Có 2 nhánh riêng: EA và điều phối (D12) |
| **Builder** | Dựng phiên bản EA mới, viết bản đối chiếu Python, chạy kiểm tra tĩnh |
| **Reviewer** | Rà mã độc lập trước khi giao bản mới |
| **Reporter** | Báo cáo cho người dùng bằng tiếng Việt: số thật, mỗi điểm yếu kèm cách sửa (F7) |

## 3. Hợp đồng dữ liệu (kênh giữa các agent)

| Từ → đến | Kênh | Nội dung |
|---|---|---|
| EA → GitHub | `repository_dispatch` (báo cáo v2) | Số liệu gộp theo M### × giai đoạn, thống kê lệnh ảo theo lý do, `mkt.ctx`, heartbeat. Không có dữ liệu từng lệnh |
| GitHub → EA | `control.json` (raw, có hạn) | risk_mult, pause_all, open_risk_pct, side, bật/tắt mã M### theo giai đoạn, news_blocks |
| GitHub → EA | `data/latest.json` | Bối cảnh vĩ mô và lịch tin (chỉ để lọc rủi ro) |
| GitHub → Claude | `reports/`, `reports/coord/`, PR cảnh báo | Thống kê và đề xuất điều phối |
| Claude → GitHub | commit `control/`, `reports/coord/` | Quyết định điều phối và lý do |
| Claude → người dùng | báo cáo, file zip EA | Phân tích, kết quả, bản nâng cấp |
| Người dùng → EA | file `.set`, `InpGhMaxRiskMult` | Giới hạn cục bộ. Điều phối không vượt được giới hạn này |

## 4. Vòng lặp điều phối

1. **Mỗi phút: EA.** Agent Market cập nhật trạng thái thị trường. Agent Signal tạo tín hiệu. Agent Risk và Control client lọc. Agent Execution vào lệnh. Agent Shadow ghi các tín hiệu bị chặn.
2. **Mỗi giờ: EA → GitHub.** Gửi heartbeat và `mkt.ctx`.
3. **Mỗi ngày: GitHub.** Agent Data và Quality cập nhật dữ liệu. Agent Analytics làm báo cáo ngày. Agent Coordinator đề xuất điều phối. Agent Alerts cảnh báo.
4. **3 lần/ngày: Claude.** Phân tích thị trường, ra 3 kịch bản, rồi quyết định điều phối và commit.
5. **Mỗi tuần: Claude.**
   - Agent Researcher chấm lại điều phối bằng lệnh ảo (`coord_live.md`).
   - Nghiên cứu bản EA mới.
   - Agent Builder và Reviewer dựng và rà bản mới. Bản mới chỉ được giao khi tốt hơn bản đang chạy (D10–D13).
6. **Phản hồi.** Lệnh ảo cho biết mỗi lần chặn hay bật lệnh là đúng hay sai. Agent Coordinator dùng kết quả này để học: bật ô tốt, tắt ô yếu (D13).

## 5. KPI chung

| KPI | Định nghĩa | Hiện tại (mô phỏng) |
|---|---|---|
| Ngày có lệnh | % ngày giao dịch có ít nhất 1 lệnh | đang đo (vòng C2) |
| Ngày có lãi | % ngày giao dịch có lãi sau phí | v1.40 D7: khoảng 4/5 ngày có lệnh không lỗ (đang đo lại chính xác) |
| Tỉ lệ thắng | sau điều phối, so với khi không điều phối | v1.40: 62,4%; mức điều phối cộng thêm tốt nhất +3–6,7 điểm, mục tiêu +10–20 |
| Lãi, sụt giảm, chuỗi thua | §9 trong research-protocol | v1.40: +123R, DD 9,5R, chuỗi thua 9 |

Không agent nào cam kết lãi mọi ngày. Các agent tối ưu để số ngày có lệnh và số ngày có lãi tăng lên, trong khi ngày lỗ được giới hạn: tối đa −3R mỗi ngày nhờ dừng lỗ ngày W1b.

## 6. Lộ trình

| Bước | Nội dung | Trạng thái |
|---|---|---|
| 1 | Tài liệu này và yêu cầu E4 | ✅ |
| 2 | Coordinator agent v2: luật Wilson, phân bổ rủi ro, thang sụt vốn, chọn chiều | 🟡 đang làm |
| 3 | Coordinator C2: bật thêm họ lệnh dự bị và yếu tố mới, KPI ngày có lệnh và ngày có lãi | 🟡 đang nghiên cứu |
| 4 | EA agent: tách rõ các agent con trong mã. Mỗi họ lệnh là một module có trạng thái, lệnh ảo và mã M### riêng; agent Risk dùng chung | sau khi chốt bản EA mới |
| 5 | Vòng học tuần từ lệnh ảo trên demo | cần người dùng cài EA lên demo |
