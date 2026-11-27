# Yêu cầu của người dùng (bắt buộc) và bảng kiểm

Claude phải đọc file này **trước mỗi lần điều phối, mỗi lần nâng cấp EA và mỗi câu trả lời quan trọng**, rồi đối chiếu từng mục để không bỏ sót.

Trạng thái: ✅ đã làm · 🟡 làm một phần hoặc đang làm · ❌ chưa làm.

Cập nhật lần cuối: 2026-11-27.

## A. Mục tiêu EA

| # | Yêu cầu | Trạng thái |
|---|---|---|
| A1 | Tỉ lệ thắng ≥ 70% ở **mỗi tháng** khi chốt trọn 1R, trên 12 tháng (12/2025–11/2026) | 🟡 Đạt trên dữ liệu đã dùng để chỉnh (v1.30: 70,3–71,9%). Kiểm tra chéo trên các tháng không dùng để chỉnh chỉ ~47% |
| A2 | Nhiều lệnh nhất có thể (≥ 124/tháng, mong muốn ≥ 500/tháng), vẫn ≥ 70% | 🟡 v1.30 ~552/tháng trên dữ liệu đã dùng để chỉnh |
| A3 | Ít nhất 3 lệnh mỗi ngày giao dịch | 🟡 Đang theo dõi trên demo |
| A4 | Tăng lệnh, tăng lãi, giảm sụt giảm, giảm lệnh thua, giảm rủi ro, giảm chuỗi thua, kéo dài chuỗi thắng | 🟡 Theo dõi hằng ngày, nâng cấp hằng tuần |
| A5 | Không lệnh thua lớn: không có chuyện 1 lệnh thua bằng 2–3 lệnh thắng (thắng và thua đều ~1R) | 🟡 EA161 chốt trọn 1R. Đo R thắng TB so với R thua TB mỗi ngày |
| A6 | Lệnh đơn, SL/TP đặt ngay khi vào lệnh, rủi ro ≤ 1%/lệnh; không DCA/lưới/martingale/hedge | ✅ |
| A7 | Phương pháp có công thức và quy luật rõ ràng kiểu Zumi; không phán đoán tùy hứng, không luật gắn với ngày cụ thể | ✅ |
| A9 | **Mọi lệnh có thẻ phương pháp**: công thức điểm vào, SL, TP, thời gian giữ, giai đoạn được phép và **xác suất thắng** đo được (trên dữ liệu đã chỉnh, kiểm tra chéo, chạy thật). Các họ phương pháp cần có: Zumi, Price Action, SMC/ICT, chỉ báo + đồng thuận đa khung, Breakout & Momentum, Volume Profile/AMT, Order Flow/Tape | 🟡 Thư viện đã có Zumi, PA, SMC (vùng), chỉ báo/đa khung, breakout/ORB. Sổ thẻ phương pháp đang soạn. AMT/Order Flow chỉ làm được bằng tick volume (CFD vàng không có khối lượng thật và sổ lệnh) |
| A10 | Số lệnh mở rộng **theo đo lường**: 550/tháng hiện tại → ~1000–1500 ứng viên → 3000–5000, chỉ khi lớp lọc chứng minh còn lợi thế | 🟡 |
| A8 | Được giữ lệnh qua cuối tuần và quá 1440 phút | ✅ |

## B. Phân tích thị trường (EA và Claude)

| # | Yêu cầu | Trạng thái |
|---|---|---|
| B1 | Nhận diện giai đoạn **đa khung** (W1/D1/H4/H1/M15): xu hướng, sức mạnh, biến động, vị trí | ✅ EA (nhãn giai đoạn, ô 15 bit) |
| B2 | Mô hình nến khung lớn (nhấn chìm, pin, inside, outside, doji, quét đỉnh/đáy) và chỉ báo khung lớn (RSI/MACD/Stoch/BB/EMA200/ADX-DI/Ichimoku) | ✅ EA (bảng thông tin) |
| B3 | Theo từng giai đoạn: tự áp dụng, điều chỉnh hoặc **tạm dừng** phương pháp; rủi ro, giới hạn và tạm dừng khi thua theo giai đoạn | ✅ Mặt nạ giai đoạn + kênh điều phối |
| B4 | **≥ 3 kịch bản tương lai** (tiếp diễn / hồi rồi tiếp / đảo chiều) có xác suất và mức giá, **dựng từ tin tức + nến + đa khung + mô hình**, dùng làm cơ sở điều phối lệnh | 🟡 Bảng thông tin của EA có 3 kịch bản, nhưng chỉ dựa trên các đoạn quá khứ tương tự (hướng đúng ~50%). Từ 27/11 Claude tự dựng 3 kịch bản ở mỗi lần điều phối, ghi vào sổ và chấm điểm lần sau. Thứ Bảy nghiên cứu bộ kịch bản v2 trong EA |
| B5 | Bộ nhớ tự học: lưu từng tháng (giai đoạn, đảo chiều, biến động, cái gì hiệu quả) và mô phỏng dựa trên các đoạn quá khứ tương tự | ✅ ZumiMem + K-NN (chỉ hiển thị) |
| B6 | Thư viện phương pháp rất lớn cho mọi chế độ thị trường, kể cả tin tức (NFP/CPI/FOMC) và **thiên nga đen** | ✅ 188 phương pháp trong v1.30 + lớp phòng khủng hoảng |
| B7 | EA đóng vai **chuyên gia phân tích**: bảng thông tin + log (giai đoạn từng khung, kịch bản, phương pháp đang bật/dừng, lý do) | ✅ |
| B8 | EA như **1 AI agent với ~15 agent con** (các họ phương pháp) + Claude điều phối phía trên | ✅ |
| B9 | **Claude phải phân tích đa khung, mô hình và tín hiệu đảo chiều, tin tức TRƯỚC mỗi quyết định điều phối** | 🟡 Đã đưa vào quy trình từ 27/11. Dữ liệu đa khung đầy đủ cần v1.31 (gửi mỗi giờ) |
| B10 | Khi thiếu dữ liệu, dùng thêm nguồn **miễn phí bên ngoài** | 🟡 Lịch kinh tế USD (tin High/Medium, tuần này và tuần sau) lấy tự động mỗi giờ → `data/calendar/ff_week.json`. Nguồn giá vàng miễn phí (stooq) chặn máy chủ GitHub, nên dữ liệu giá đa khung lấy từ EA (v1.31) |
| B11 | Claude chủ động đề xuất và bổ sung cho EA những gì cần thêm, mục tiêu **có lợi nhuận mỗi ngày** | 🟡 v1.31 thêm 2 lớp bảo vệ (giới hạn lệnh cùng chiều dồn dập, chặn lệnh ngược tín hiệu đảo chiều H1/H4). **KHÔNG khóa lãi trong ngày** (người dùng: có tín hiệu là vào lệnh, điều phối cho tốt). Mặc định TẮT; kiểm tra trên 12 tháng + kiểm tra chéo rồi mới bật |
| B12 | Đo hiệu quả điều phối: kết quả **lệnh ảo** của các tín hiệu bị chặn (theo phương pháp, giai đoạn, lý do chặn). Thứ tự đã thống nhất: đo lệnh ảo → lớp lọc từng tín hiệu trong EA → rồi mới tăng lên 1000–5000 tín hiệu/tháng | 🟡 Đang làm v1.32 |

## C. Điều phối (Claude qua GitHub sakura)

| # | Yêu cầu | Trạng thái |
|---|---|---|
| C1 | Claude toàn quyền điều phối trong giới hạn cứng của EA; mục tiêu lợi nhuận tốt nhất mỗi ngày, không bỏ phí cơ hội | ✅ |
| C2 | Vai trò bắt buộc: trader lướt sóng và trong ngày chuyên nghiệp, nhà kinh doanh, nhà toán học, nhà nghiên cứu; khai thác mọi lợi thế có số liệu chứng minh | ✅ Ghi trong quy trình |
| C3 | Điều phối 3 lần/ngày trước phiên Á/Âu/Mỹ (6:47, 13:47, 19:47 VN); cảnh báo lớn thì xử lý ngay | ✅ |
| C4 | EA nhận lệnh điều phối nhanh (mỗi 5 phút) | ✅ |
| C5 | Claude chỉnh rủi ro theo thị trường: hệ số rủi ro, trần rủi ro đang mở (≤ mức người dùng đặt, mặc định 12%), dừng hoặc bật phương pháp, chặn giờ tin | ✅ |
| C6 | Mỗi ngày tìm hướng cải thiện (nhật ký cải tiến) để cuối tuần nâng cấp tốt hơn | ✅ Từ 27/11 |
| C7 | Báo cáo bằng tiếng Việt sau mỗi lần điều phối: phân tích thị trường, lệnh, thay đổi và lý do, kế hoạch; hỏi người dùng | ✅ |
| C8 | Không mở hoặc đóng lệnh từ xa; không đổi SL/TP; không nới giới hạn cứng (giới hạn an toàn) | ✅ |
| C9 | **Claude toàn quyền loại bỏ cái không tốt** (phương pháp, bộ lọc, tính năng) khi số liệu chứng minh: chạy thật đủ mẫu thua rõ so với mô hình, kiểm tra chéo không có lợi thế, hoặc lệnh ảo cho thấy bộ lọc chặn nhầm lệnh tốt. Báo người dùng sau khi làm | ✅ Đã bỏ: khóa lãi trong ngày; 2 lớp bảo vệ v1.31 (không có lợi thế) gỡ khỏi v1.32 |
| C10 | **Định hướng chiều lệnh theo tiêu chí đa khung**: đủ tiêu chí (EMA/MA, Ichimoku, RSI, MACD, ADX, cấu trúc HH/HL–BOS/CHoCH, trendline, Fibo, nến đảo chiều tại S/R, phân kỳ, tin tức) trên W1/D1/H4/H1/M15/M5 thì chỉ cho Sell khi giảm đa khung, chỉ Buy khi tăng đa khung. Claude đặt `global.side` = buy/sell/both/auto qua GitHub; auto = EA tự chấm điểm đồng thuận mỗi nến | 🟡 v1.33 đã dựng (chờ biên dịch): cầu nối 2.6 đọc `global.side`, EA chặn lệnh MỚI ngược chiều, `auto` theo điểm CDS (hiện trên bảng + mkt.ctx), lệnh bị chặn ghi lý do SIDE. Kiểm tra chéo: CDS KHÔNG có lợi thế chiều (AUC ~0,51) -> dùng như công cụ giảm rủi ro, không phải tín hiệu |
| C11 | Vào lệnh khung **M1** (M1/M5), định hướng khung lớn | ✅ Nguyên tắc nghiên cứu |
| C12 | Điều kiện nào thiếu để điều phối có lãi thì báo người dùng hoặc tự bổ sung | ✅ Thường xuyên |

## D. Nâng cấp EA

| # | Yêu cầu | Trạng thái |
|---|---|---|
| D1 | Tự nâng cấp khi cần rồi báo người dùng. Mọi nâng cấp phải được nghiên cứu, **không được bỏ qua** | ✅ |
| D2 | Nâng cấp hằng tuần vào thứ Bảy 9:17 VN (từ 5/12) | ✅ Đã lên lịch |
| D3 | Tên file có số phiên bản (vd. `_v130_`) để không ghi đè bản cũ | ✅ |
| D4 | Không thấy báo cáo "không đạt" mà dừng: phải nghiên cứu thêm phương pháp cho đến khi đạt | ✅ Nguyên tắc làm việc |
| D5 | Kiểm tra trước khi gửi: đối chiếu với mô hình, kiểm tra tĩnh, soát độc lập, số input ≤ 1024, chuỗi input ≤ 255 ký tự, biên dịch 0 lỗi và 0 cảnh báo | ✅ |

## E. Demo → tiền thật

| # | Yêu cầu | Trạng thái |
|---|---|---|
| E1 | Demo 2 tuần (v1.30 tuần 1, bản nâng cấp 5/12 tuần 2). Đánh giá **12/12** | 🟡 Đang chạy |
| E2 | Điều kiện: ngày nào cũng có lệnh; lãi > 0, hệ số lợi nhuận ≥ 1,2; sụt giảm ≤ 10%, không tuần nào lỗ quá 5%; ~≥ 200 lệnh; báo trung thực tỉ lệ thắng so với 70% | 🟡 |
| E3 | Đạt thì vào tiền thật bằng tài khoản nhỏ, rủi ro 0,25–0,5% mỗi lệnh | ❌ Chờ 12/12 |

## F. Cách làm việc

| # | Yêu cầu | Trạng thái |
|---|---|---|
| F1 | Luôn trả lời bằng tiếng Việt | ✅ |
| F2 | Trung thực, không bịa số; báo cả số kiểm tra chéo và số chạy thật | ✅ |
| F3 | Tiết kiệm token để dùng đủ cả tuần (trả lời ngắn, tối đa 1 tác tử) | ✅ |
| F4 | Kiên nhẫn, không làm ẩu; ý tưởng của người khác chỉ dùng để tham khảo, không giảng giải triết lý giao dịch | ✅ |
| F5 | Bảo mật: không đưa token, số tài khoản, email lên repo hay vào chat; repo công khai chỉ nhận báo cáo tổng hợp | ✅ |
| F6 | **Rà soát file này trước khi điều phối hoặc nâng cấp; mục nào 🟡 hoặc ❌ thì nêu trong báo cáo** | ✅ |
| F7 | Cách báo cáo: **hướng giải pháp**. Mỗi điểm yếu luôn đi kèm cách cải thiện cụ thể và việc đang làm; không chỉ nói "không tốt". Số liệu vẫn phải thật | ✅ |
