# Sổ nguồn nghiên cứu ngoài (Điều 5)

Đây là danh sách các nguồn bên ngoài đã đọc hoặc đã thử. **Nguồn nào đã có trong sổ thì không đọc lại**, trừ khi có phiên bản mới.
- Mỗi dòng ghi: ngày, nguồn, loại (dữ liệu / chiến lược / mã / cộng đồng), kết luận, đã dùng ở đâu.
- Chỉ ghi tên nguồn và URL công khai. Không ghi tài khoản, token hay dữ liệu riêng.
- Repo này công khai, nên không chép nội dung có bản quyền vào đây. Chỉ tóm tắt 1 dòng.

| Ngày | Nguồn | Loại | Kết luận | Dùng ở |
|---|---|---|---|---|
| 2026-11 | FRED: VIX, DFII10, DTWEXBGS | dữ liệu | Không có lợi thế ngoài mẫu cho lệnh 1R vàng (nghiên cứu macro1, xác nhận lại ở C2). Chỉ dùng làm bối cảnh và lọc rủi ro | `data/latest.json` |
| 2026-11 | Stooq (vàng D1/W1) | dữ liệu | Chặn runner GitHub nên đã tắt | `scripts/gold.py` |
| 2026-11 | Lịch Forex Factory (nfs.faireconomy.media) | dữ liệu | Dùng cho tin USD mức High/Medium | `scripts/ff_calendar.py` |
| 2026-11 | Price Action, SMC/ICT, Fibo, RSI, MA/EMA, trendline (người dùng đề xuất) | chiến lược | Đã thử dưới dạng luật trong v1.30–v1.38. Chỉ S4, HTX, TD giữ được lợi thế ngoài mẫu | các vòng rx/ea161v13x |
| 2026-11-27 | FRED GVZCLS (chỉ số biến động vàng CBOE) | dữ liệu | Theo ngày từ 2008, trễ 1 ngày. Có bằng chứng dự báo biến động thực ngoài mẫu. Chỉ dùng để chỉnh cỡ SL hoặc chọn chế độ, không phải tín hiệu hướng | v1.45: SL theo GVZ cho TD được nhận; cổng VRP bị loại |
| 2026-11-27 | CFTC COT API (vàng mã 088691) | dữ liệu | Theo tuần, miễn phí. Chỉ là bối cảnh tuần, giá trị thấp cho M1 | chưa dùng |
| 2026-11-27 | Lượng vàng GLD (file lịch sử xlsx); trang IAU | dữ liệu | Theo ngày, dòng tiền chậm, chỉ là bối cảnh | chưa dùng |
| 2026-11-27 | Tick Dukascopy (dukascopy-node, duka, các repo tick XAUUSD 2024–26) | dữ liệu | Có bid/ask miễn phí theo UTC. Hợp để mô hình spread và kiểm M1 ngoài mẫu. Kiểm tra điều khoản; không commit dữ liệu thô | nghiên cứu offline |
| 2026-11-27 | CME daily volume; FinzData | dữ liệu | Chỉ có khối lượng tổng ngày. OI đầy đủ phải trả phí | không dùng |
| 2026-11-27 | WGC Goldhub (dự trữ vàng ngân hàng trung ương) | dữ liệu | Theo tháng, trễ 2 tháng | không dùng |
| 2026-11-27 | Blose & Gondhalekar 2014/2017 (lợi nhuận qua đêm) | chiến lược | Qua đêm dương, trong ngày âm (1985–2012). Hiệu ứng yếu dần, không có kiểm định ngoài mẫu đúng nghĩa | giả thuyết H2 |
| 2026-11-27 | Wei 2026, SSRN 7257240 (phân rã theo phiên) | chiến lược | Phiên Á tạo phần lớn lợi nhuận 2024–26. Chỉ trong mẫu, giai đoạn vàng tăng mạnh | v1.45 H2: loại (chỉ có ở D7) |
| 2026-11-27 | Quantex WP03 2026 (hiệu ứng lịch, 44 giả thuyết) | chiến lược | Chỉ đảo dấu nến ngày sống được ngoài mẫu. Giờ, thứ, tháng đều không | v1.45 H3: loại |
| 2026-11-27 | Awartani và cộng sự (vàng 5 phút quanh FOMC) | chiến lược | Phản ứng thiếu 5–10 phút sau cú sốc FOMC. Chỉ trong mẫu 2007–2020 | v1.45 H5: loại (âm cả 2 giai đoạn) |
| 2026-11-27 | Xu 2020; Bouri/Ma 2025 (động lượng trong ngày) | chiến lược | R² khoảng 0,5%, quá nhỏ cho lệnh 1R | không dùng |
| 2026-11-27 | London PM fix (Abrantes-Metz) | chiến lược | Hiệu ứng hết từ khoảng 2013 | không dùng |
| 2026-11-27 | github.com/apurv-wicko/gold-strategy-tests | mã | 16 setup phổ biến trên M1 2021–26, có phí: hầu hết âm. Nguyên nhân chính là phí so với khoảng SL; lọc SL ≥ 10% ADR14 cứu được phần lớn | v1.45 H1: nhận cho S4 ngày tin và TD H1/H2; loại cho T5/T15/LOR/AFD/PDS/NYC/TPB |
| 2026-11-27 | EA vàng trên GitHub (xauusd-trend-follow, HydraPrime, FvgGold-EA, BAKOMEGoldScalper, GoldTraderEA) | mã | Tối ưu trong mẫu, không có kiểm định ngoài mẫu, không đáng tin | không dùng |
| 2026-11-27 | r/algotrading; blog YuRa Trading | cộng đồng | Breakout và mua dip vàng thường sụp khi tính phí. Ý tưởng lặp lại: phiên Á, lọc spread/ATR | giả thuyết |
| 2026-11-27 | Telonics; Delta Terminal (nghiên cứu sweep thanh khoản) | chiến lược | Sweep không đảo chiều. Phá đỉnh/đáy tuần trước rồi đi tiếp 1 ATR: 61,9% so với 50,3% ở mức giả, trên 7 thị trường, giữ được ngoài mẫu 2025–26. Chưa tính phí | ứng viên PWB, khoảng 3–4 lệnh/tháng |
| 2026-11-27 | Thinridge: fade đỉnh/đáy tuần trước, inside-bar, squeeze (GC) | chiến lược | Âm sau phí, 2008–26 | không dùng |
| 2026-11-27 | ripbacktest.com (21 chiến lược vàng, TTM Squeeze) | chiến lược | Không bản nào qua cổng. Thêm SL/TP cố định làm hỏng các luật xu hướng | cảnh báo |
| 2026-11-27 | github.com/Kidus-M/Quant; AaroNLaU0307/quant-trend-research; wickcheck Donchian D1 | mã | Donchian H4 có lọc phiên: walk-forward PF 1,42, nhưng chỉ ở phân vị 90 so với vào ngẫu nhiên. Lợi thế phụ thuộc trailing | ứng viên yếu |
| 2026-11-27 | xauusdglobal NR7; pinescriptforge GC NR7 | chiến lược | NR7 D1: 71% ngày sau đóng ngoài biên, nhưng 25% phá cả hai phía. Không kiểm ngoài mẫu | ứng viên yếu |
| 2026-11-27 | xauusdglobal weekly open | chiến lược | 53 lệnh trong 2 năm, PF 1,69 ở mục tiêu 2R. Năm 2025 âm | ứng viên yếu |
| 2026-11-27 | Turn-of-month (guptagaurav, quantarenas, In Gold We Trust 2024, Kumar 2024) | chiến lược | Bằng chứng yếu, p=0,02 nhưng là một trong nhiều phép thử | yếu |
| 2026-11-27 | Gap đầu tuần (forgestrategylab, quorailab, …) | chiến lược | Chỉ hơn mức gốc 3,6 điểm; 58% số gap nhỏ hơn spread | loại |
| 2026-11-27 | Tỷ lệ vàng/bạc (GlobalARB, tickfloor, quantarenas) | chiến lược | Walk-forward ngoài mẫu −0,27%/năm | loại |
| 2026-11-27 | Connors RSI(2)/IBS D1 trên GLD (bulltides, duckquant, setup4alpha, quantifiedstrategies) | chiến lược | Thua mua-giữ ở cả hai nửa mẫu | loại |
| 2026-11-27 | tesseratrades BB breakout NY H1 | chiến lược | Mục tiêu 5R, thắng 24%, không chuyển sang 1R được | không dùng |
