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
| **Reporter** | Gửi báo cáo gộp v2, heartbeat mỗi giờ và thống kê lệnh ảo lên GitHub; từ cầu nối 2.7 thêm `by_side` (mua / bán, cho luật R09) và `by_session` (ASIA / LON / NY / OFF) | cầu nối 2.7 |
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
| **Monitor** | Tự giám sát: mỗi agent và agent con một dòng OK / WARN / FAIL kèm lý do, cùng KPI ngày (số lệnh hôm nay, số ngày có lệnh và có lãi trong 7 / 30 ngày, chuỗi thua, sụt giảm tuần / tháng) | `agents_status.py` → `reports/agents/` (trong `data.yml`) |

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
| Ngày có lệnh | % ngày giao dịch có ít nhất 1 lệnh | v1.40 D7: 53% (C2); "I1 only" v1.43: 74% |
| Ngày có lãi | % ngày giao dịch có lãi sau phí | v1.40 D7: 30% số ngày giao dịch (≈57% số ngày có lệnh); "I1 only": 37% |
| Tỉ lệ thắng | sau điều phối, so với khi không điều phối | v1.40: 62,4%; mức điều phối cộng thêm tốt nhất +3–6,7 điểm, mục tiêu +10–20 |
| Lãi, sụt giảm, chuỗi thua | §9 trong research-protocol | v1.40: +123R, DD 9,5R, chuỗi thua 9 |

Không agent nào cam kết lãi mọi ngày. Các agent tối ưu để số ngày có lệnh và số ngày có lãi tăng lên, trong khi ngày lỗ được giới hạn: tối đa −3R mỗi ngày nhờ dừng lỗ ngày W1b.

## 6. Vận hành như một công ty (yêu cầu E5)

Cả hệ thống được vận hành như một công ty. Giao dịch là hoạt động **khai thác dòng tiền**. Mỗi agent là một phòng ban, có KPI riêng. KPI được đo hằng ngày hoặc hằng tuần và so với kỳ trước, để mỗi agent tốt lên dần.

| Phòng ban (agent) | Vai trò trong công ty | KPI chính | Mục tiêu | Nguồn đo |
|---|---|---|---|---|
| **Ban điều hành** (Claude Coordinator) | Phân bổ vốn rủi ro và quyết định điều phối | Mức nâng tỉ lệ thắng của lệnh được cho qua so với lệnh bị chặn (lệnh ảo CTRL); `control.json` luôn còn hạn | +10–20 điểm; 0 lần quá hạn 48 h | lệnh ảo, `agents_status` |
| **Phân tích** (Claude Analyst, EA Market) | Dự báo thị trường | Tỉ lệ đúng của 3 kịch bản (chấm theo sổ kịch bản) | tăng dần, mốc > 50% | `scenario_ledger.csv` |
| **Sản xuất** (EA Signal) | Tạo tín hiệu có lợi thế | Số lệnh/tháng, % ngày có lệnh, lãi R mỗi lệnh | +20% lệnh mỗi bản (A14), tiến tới 100/tháng | báo cáo gộp |
| **Quản trị rủi ro** (EA Risk) | Giữ vốn | DD tuần/tháng, ngày lỗ nặng nhất, chuỗi thua, 0 vi phạm giới hạn cứng | DD ≤ bản trước; ngày ≥ −3R; 0 vi phạm | báo cáo gộp, `rule` |
| **Vận hành** (EA Execution) | Khớp lệnh sạch | Trượt giá, lệnh lỗi, lệnh trùng, spread lúc vào | 0 lệnh trùng; lỗi giảm dần | nhật ký EA |
| **Kiểm toán** (EA Shadow) | Đo chặn/bật đúng hay sai | Số lệnh ảo mỗi ô (n ≥ 30) | đủ mẫu sau 1–3 tháng | lệnh ảo |
| **Dữ liệu** (GitHub Data + Quality) | Cung cấp dữ liệu | % ngày dữ liệu đủ và mới | 100% | `quality.json` |
| **Tài chính** (GitHub Analytics) | Sổ sách | Báo cáo ngày/tuần/tháng đúng hạn | 100% | `reports/` |
| **Pháp chế** (GitHub Guard) | Kiểm soát tuân thủ | `control.json` hợp lệ; 0 dữ liệu riêng tư lọt vào repo công khai | 100%; 0 | `control.py`, `agg.py` |
| **R&D** (Claude Researcher, Builder, Reviewer) | Phát triển sản phẩm | Mỗi bản mới tốt hơn bản đang chạy theo §9; số lỗi tìm được khi rà mã | đạt luật dựng; lỗi giảm dần | báo cáo vòng nghiên cứu |
| **Quan hệ chủ sở hữu** (Claude Reporter) | Báo cáo cho người dùng | Báo cáo đúng số liệu, mỗi điểm yếu kèm hướng sửa (F7) | 100% | — |

**Chỉ tiêu cấp công ty** (dòng tiền):
- lãi ròng R/tuần và R/tháng;
- % ngày có lệnh;
- % ngày có lãi;
- DD tháng;
- tỉ lệ thắng sau điều phối.

`agents_status.py` theo dõi các KPI đo được từ repo. Mỗi tuần, Claude chấm toàn bộ bảng KPI, so với tuần trước và ghi hướng cải tiến cho từng phòng ban vào `improve_log`.

## 7. Lộ trình

| Bước | Nội dung | Trạng thái |
|---|---|---|
| 1 | Tài liệu này và yêu cầu E4 | ✅ |
| 2 | Coordinator agent v2: luật Wilson, phân bổ rủi ro, thang sụt vốn, chọn chiều | ✅ (c54b356) |
| 3 | Coordinator C2: bật thêm họ lệnh dự bị và yếu tố mới, KPI ngày có lệnh và ngày có lãi | ✅ nghiên cứu xong: trung tính, chưa đạt +10 điểm |
| 3b | Cầu nối 2.7 (by_side, by_session) và giám sát agent (`agents_status.py`) | ✅ (f4772ff) |
| 3c | KPI theo từng phòng ban (mục 6) trong `agents_status.py`, có xu hướng so với kỳ trước | 🟡 đang làm |
| 4 | EA agent: tách rõ các agent con trong mã. Mỗi họ lệnh là một module có trạng thái, lệnh ảo và mã M### riêng; agent Risk dùng chung | sau khi chốt bản EA mới |
| 5 | Vòng học tuần từ lệnh ảo trên demo | cần người dùng cài EA lên demo |
