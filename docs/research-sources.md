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
