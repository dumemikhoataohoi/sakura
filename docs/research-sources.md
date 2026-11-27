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
| 2026-11-27 | FRED GVZCLS (chỉ số biến động vàng CBOE) | dữ liệu | Theo ngày từ 2008, trễ 1 ngày. Có bằng chứng dự báo biến động thực ngoài mẫu. Chỉ dùng để chỉnh cỡ SL hoặc chọn chế độ, không phải tín hiệu hướng | dự kiến (giả thuyết H4) |
| 2026-11-27 | CFTC COT API (vàng mã 088691) | dữ liệu | Theo tuần, miễn phí. Chỉ là bối cảnh tuần, giá trị thấp cho M1 | chưa dùng |
| 2026-11-27 | Lượng vàng GLD (file lịch sử xlsx); trang IAU | dữ liệu | Theo ngày, dòng tiền chậm, chỉ là bối cảnh | chưa dùng |
| 2026-11-27 | Tick Dukascopy (dukascopy-node, duka, các repo tick XAUUSD 2024–26) | dữ liệu | Có bid/ask miễn phí theo UTC. Hợp để mô hình spread và kiểm M1 ngoài mẫu. Kiểm tra điều khoản; không commit dữ liệu thô | nghiên cứu offline |
| 2026-11-27 | CME daily volume; FinzData | dữ liệu | Chỉ có khối lượng tổng ngày. OI đầy đủ phải trả phí | không dùng |
| 2026-11-27 | WGC Goldhub (dự trữ vàng ngân hàng trung ương) | dữ liệu | Theo tháng, trễ 2 tháng | không dùng |
| 2026-11-27 | Blose & Gondhalekar 2014/2017 (lợi nhuận qua đêm) | chiến lược | Qua đêm dương, trong ngày âm (1985–2012). Hiệu ứng yếu dần, không có kiểm định ngoài mẫu đúng nghĩa | giả thuyết H2 |
| 2026-11-27 | Wei 2026, SSRN 7257240 (phân rã theo phiên) | chiến lược | Phiên Á tạo phần lớn lợi nhuận 2024–26. Chỉ trong mẫu, giai đoạn vàng tăng mạnh | giả thuyết H2 |
| 2026-11-27 | Quantex WP03 2026 (hiệu ứng lịch, 44 giả thuyết) | chiến lược | Chỉ đảo dấu nến ngày sống được ngoài mẫu. Giờ, thứ, tháng đều không | giả thuyết H3 |
| 2026-11-27 | Awartani và cộng sự (vàng 5 phút quanh FOMC) | chiến lược | Phản ứng thiếu 5–10 phút sau cú sốc FOMC. Chỉ trong mẫu 2007–2020 | giả thuyết H5 |
| 2026-11-27 | Xu 2020; Bouri/Ma 2025 (động lượng trong ngày) | chiến lược | R² khoảng 0,5%, quá nhỏ cho lệnh 1R | không dùng |
| 2026-11-27 | London PM fix (Abrantes-Metz) | chiến lược | Hiệu ứng hết từ khoảng 2013 | không dùng |
| 2026-11-27 | github.com/apurv-wicko/gold-strategy-tests | mã | 16 setup phổ biến trên M1 2021–26, có phí: hầu hết âm. Nguyên nhân chính là phí so với khoảng SL; lọc SL ≥ 10% ADR14 cứu được phần lớn | giả thuyết H1 |
| 2026-11-27 | EA vàng trên GitHub (xauusd-trend-follow, HydraPrime, FvgGold-EA, BAKOMEGoldScalper, GoldTraderEA) | mã | Tối ưu trong mẫu, không có kiểm định ngoài mẫu, không đáng tin | không dùng |
| 2026-11-27 | r/algotrading; blog YuRa Trading | cộng đồng | Breakout và mua dip vàng thường sụp khi tính phí. Ý tưởng lặp lại: phiên Á, lọc spread/ATR | giả thuyết |
