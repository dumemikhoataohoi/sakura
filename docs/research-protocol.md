# Quy trình nghiên cứu và phát triển EA (bắt buộc, người dùng ban hành 27/11/2026)

Mọi đợt nghiên cứu hoặc nâng cấp EA161 phải theo đúng quy trình này. Bản tóm tắt dưới đây giữ nguyên ý của người dùng. Mỗi báo cáo phải đi đúng thứ tự các mục.

## 0. Trước khi làm
Đọc lại toàn bộ trao đổi và kết quả **4 ngày gần nhất**:
- phương pháp đã nghiên cứu và đã bị loại, kèm lý do;
- các thử nghiệm chỉ cải thiện khoảng 1%;
- lọc 12 tháng, theo trạng thái thị trường, đa khung.

Không lặp lại hướng đã thất bại nếu không có **giả thuyết mới** giải thích vì sao lần này có thể khác.

## 1. Mục tiêu (mục tiêu nghiên cứu, không tự nhận đã đạt)
- Tỷ lệ thắng từ 70–80% trở lên.
- Hướng tới ít nhất 500 lệnh/tháng nếu dữ liệu cho phép.
- Lãi tăng.
- Giảm ngày, tuần, tháng lỗ, giảm DD, chuỗi thua và rủi ro.

Cấm làm đẹp tỷ lệ thắng bằng cách che lỗ, bỏ sót lệnh, giữ lệnh thua vô hạn hoặc martingale.

Thử **ít nhất 2–3 phương pháp hệ thống** có tiềm năng nhiều lệnh. Mỗi phương pháp là công thức cụ thể, không chọn lệnh theo cảm tính.

## 2. Chốt dữ liệu và bản đối chứng
Ghi rõ:
1. EA đối chứng: tên, phiên bản, đường dẫn, mã nguồn.
2. Thông số, symbol, khung thời gian, vốn, đòn bẩy, spread, commission, swap, trượt giá.
3. Nguồn dữ liệu tick/nến, khoảng thời gian, tỷ lệ tick thật, các khoảng thiếu.
4. Kết quả gốc: số lệnh, lãi ròng, tỷ lệ thắng, PF, DD balance và equity, ngày/tuần/tháng lỗ, chuỗi thắng và thua dài nhất.

Đếm theo **position đã đóng**, có quy tắc nhất quán cho đóng một phần. Lãi là **lãi ròng sau chi phí**.

Giữ nguyên bản gốc. Bản nghiên cứu đặt tên riêng và có nhật ký thay đổi.

## 3. Phương pháp trước điều phối
Thử phương pháp hiện tại và 2–3 nhóm bổ sung, ví dụ: trend pullback, range/reversal, breakout/retest, scalp đa khung.

Mỗi phương pháp phải ghi thành công thức lập trình được:
1. Thị trường và khung áp dụng.
2. Điều kiện BUY, SELL và khi nào không giao dịch.
3. Chỉ báo, tham số, cách kết hợp.
4. Điều kiện vào, SL, TP, thoát lệnh, quản lý lệnh.
5. Thời điểm từng dữ liệu được biết. Đỉnh/đáy, trendline, phân kỳ chỉ dùng khi đã xác nhận; không dùng dữ liệu tương lai.
6. Vì sao phương pháp có thể hiệu quả, và khi nào dễ thất bại.

Thử từng phương pháp **độc lập trước**, chưa ghép bộ lọc.

## 4. Lọc và tối ưu từng phương pháp: walk-forward 12 tháng
1. Dùng 12 tháng quá khứ để chọn tham số và bộ lọc.
2. Khóa tham số, kiểm tra tháng kế tiếp.
3. Trượt cửa sổ và lặp lại, qua nhiều tháng kiểm tra liên tiếp. Ghi rõ số tháng thực sự kiểm tra được.
4. Có một **giai đoạn kiểm tra cuối** chưa từng dùng. Nếu giai đoạn đó đã bị dùng, phải nói rõ.

Không chọn tham số theo chính tháng đang kiểm tra. Không gọi là ngoài mẫu khi đã tối ưu trên chính dữ liệu đó.

Báo cho từng phương pháp và biến thể:
- số lệnh, lãi ròng, R/lệnh, tỷ lệ thắng, PF;
- ngày/tuần/tháng lỗ, DD balance và equity, chuỗi thua;
- số lệnh trung bình mỗi ngày và mỗi tháng;
- kết quả từng tháng, số tháng dương và âm;
- độ nhạy theo tham số và theo chi phí;
- cỡ mẫu. Nhóm ít lệnh ghi **chưa đủ bằng chứng**.

Phương pháp ít lệnh nhưng tốt thì giữ làm bổ trợ, không trình bày là đạt mục tiêu nhiều lệnh.

## 5. Trạng thái thị trường
Các trạng thái tối thiểu: xu hướng tăng, xu hướng giảm, đi ngang, breakout, không rõ hoặc đang chuyển trạng thái.

Đây là **giả thuyết cần kiểm tra**. Phải quy định rõ:
- cách tính và ngưỡng của từng chỉ báo;
- cách xác định đỉnh/đáy, range, breakout;
- cách xử lý khi nhiều trạng thái cùng xuất hiện hoặc trạng thái thay đổi;
- cập nhật theo tick hay theo nến đóng;
- cách xử lý khi thiếu dữ liệu.

Lập bảng **trạng thái × phương pháp × BUY/SELL** trên dữ liệu ngoài mẫu, gồm số lệnh, lãi ròng, tỷ lệ thắng, DD và cỡ mẫu.

## 6. Điều phối đa tầng (không dùng một điểm tổng duy nhất)
Mỗi tầng ghi rõ đầu vào, đầu ra và thứ tự xử lý:
1. **Chọn phương pháp:** chỉ phương pháp đã qua walk-forward 12 tháng.
2. **Chọn theo trạng thái:** trạng thái không rõ phải có quy tắc cụ thể, không chọn ngẫu nhiên.
3. **Hướng theo khung phù hợp:** để thử, không áp đặt là tối ưu (vào M1 xem M15/H1; vào M5 xem H1; vào M15 xem H4). Quy định cách xử lý khi các khung cùng hoặc ngược hướng, khi có đảo chiều hợp lệ ngược khung lớn, và chỉ dùng nến đã đóng.
4. **Xác nhận thiết lập H1/M30/M15:** thử nhiều mức kết hợp, không bắt buộc tất cả khung cùng thuận.
5. **Điểm vào M5/M1:** trigger cụ thể gồm giá, nến/tick, spread, thời điểm, điều kiện hủy. Nghiên cứu riêng các trường hợp:
   - SELL sau nhịp giảm mạnh rồi giá bật lên;
   - BUY sau nhịp tăng mạnh rồi giá quay đầu;
   - lệnh đi thuận rồi đảo chiều chạm SL.
   Đo cả **lệnh thua tránh được** lẫn **lệnh thắng bị bỏ lỡ**.
6. **Phiên, tin, spread, biến động:** tin chỉ dùng khi đã được biết tại thời điểm quyết định; xử lý đúng múi giờ và DST. Mỗi bộ lọc đo riêng.
7. **Nhiều tín hiệu cùng lúc:**
   - ưu tiên theo tiêu chí đã kiểm chứng;
   - quy tắc cho tín hiệu cùng hướng và ngược hướng;
   - kiểm soát tổng rủi ro và các lệnh trùng ý tưởng;
   - không đếm trùng thông tin;
   - điểm chất lượng phải kiểm tra ngoài mẫu.

## 7. Đo riêng tác dụng từng tầng
Thứ tự đo:
1. Đối chứng.
2. Từng phương pháp.
3. + trạng thái.
4. + hướng.
5. + thiết lập.
6. + trigger.
7. + phiên/tin.
8. + điều phối giữa các phương pháp.
9. Bản cuối.

Mỗi bước báo theo bảng:

| Bước | Tín hiệu trước lọc | Lệnh còn lại | Lãi ròng | Win rate | DD equity | Chuỗi thua | Ngày/tuần/tháng lỗ |
|---|---:|---:|---:|---:|---:|---:|---:|

Kèm theo: số lệnh thắng và thua bị loại, lãi/lỗ ước tính của chúng, và lý do loại. Lệnh giả lập phải ghi là **mô phỏng** và dùng cùng chi phí, cùng quy tắc khớp lệnh.

Không giữ một tầng chỉ vì nó tăng win rate nếu làm xấu lợi nhuận, số lệnh hoặc DD. Có đánh đổi thì đưa số liệu để người dùng quyết định.

## 8. Kiểm thử công bằng
- Cùng dữ liệu, vốn, chi phí và cách tính cho mọi bản.
- Thử trên nhiều giai đoạn thị trường.
- Thử chi phí tăng, spread rộng, trượt giá.
- Ghi số phương án đã thử.
- Đọc tỷ lệ thắng cùng kỳ vọng, PF, DD và cỡ mẫu.
- Xét tác động của mức vốn và lot tối thiểu.
- Số liệu ước lượng hoặc mô phỏng phải ghi rõ. Chỉ gọi là "backtest MT5" khi đã chạy MT5.
- Không tuyên bố live sẽ khớp backtest.

## 9. EA mới và báo cáo
EA phiên bản mới cần có:
- mã nguồn;
- phương pháp và các tầng **bật/tắt được** để kiểm tra riêng;
- bộ tham số và hướng dẫn;
- nhật ký ghi phương pháp, trạng thái, hướng, tầng, và lý do vào hoặc bỏ tín hiệu.

Báo cáo **trước khi người dùng chạy** theo bảng:

| Chỉ số | EA đối chứng | EA mới | Chênh lệch |
|---|---:|---:|---:|
| Số lệnh | | | |
| Lãi ròng | | | |
| Tỷ lệ thắng | | | |
| PF | | | |
| DD balance | | | |
| DD equity | | | |
| Chuỗi thua dài nhất | | | |
| Ngày lỗ | | | |
| Tuần lỗ | | | |
| Tháng lỗ | | | |

Ghi riêng:
1. Phương pháp đã thử, công thức và kết quả.
2. Phương pháp giữ hay loại, kèm lý do và số liệu.
3. Tác dụng từng tầng.
4. Kết quả trong mẫu, ngoài mẫu, giai đoạn kiểm tra cuối.
5. Đã làm tới bước nào: biên dịch, mô phỏng, backtest MT5, demo.
6. Mục tiêu đã đạt, chưa đạt, và phần còn phải kiểm chứng.

Chưa đạt thì tiếp tục nghiên cứu theo hướng có cơ sở. Không sửa số liệu. Không tự triển khai lên tài khoản thật.

## 10. Bổ sung mục tiêu thiết kế (người dùng, 27/11/2026)

EA phải **thích ứng** với thị trường thay đổi: có nhiều phương pháp được nghiên cứu riêng, rồi dùng phân tích đa khung để nhận biết thị trường và điều phối phương pháp phù hợp. Không dùng vài phương pháp cố định cho mọi điều kiện.

### 10.1 Vai trò của từng khung
- **Khung lớn:** bối cảnh, xu hướng, trạng thái (tăng / giảm / đi ngang / đang chuyển).
- **Khung trung gian:** giá đang tiếp diễn, điều chỉnh hay có khả năng đảo chiều.
- **Khung nhỏ:** điểm vào cụ thể, tránh mua đuổi đỉnh hoặc bán đuổi đáy.

Khi các khung ngược hướng, phải có quy tắc phân biệt **nhịp điều chỉnh** với **khả năng đổi xu hướng**. Không mặc định chặn mọi lệnh ngược khung lớn; kiểm tra từng trường hợp bằng dữ liệu.

Thử nhiều tổ hợp khung (ví dụ H1–M15–M1) và so sánh với các tổ hợp khác cũng như với EA **không** dùng điều phối đa khung. Chỉ dùng thông tin đã có tại thời điểm vào lệnh.

### 10.2 Phương pháp và điều phối
Nghiên cứu từng phương pháp độc lập trước. Mỗi phương pháp có công thức gồm: điều kiện thị trường, chỉ báo hoặc cấu trúc giá, hướng, điểm vào, SL, TP, điều kiện thoát.

Sau khi lọc 12 tháng, kiểm tra phương pháp nào hợp với trạng thái nào. Khi ghép vào EA phải quy định rõ:
- chọn phương pháp theo trạng thái và đa khung như thế nào;
- xử lý khi nhiều phương pháp cùng ra tín hiệu hoặc ra tín hiệu ngược hướng;
- xử lý khi trạng thái chưa rõ;
- giới hạn lệnh trùng ý tưởng và tổng rủi ro;
- lý do chọn phương pháp, vào lệnh hoặc bỏ qua tín hiệu (ghi nhật ký).

Đo riêng từng tầng và so với từng phương pháp chạy cố định. Không mặc định rằng thêm tầng là tốt hơn.

### 10.3 Tiêu chí chọn bản mới
- **Chốt tiêu chí trước khi tối ưu:** lãi ròng, số lệnh, tỷ lệ thắng, DD, chuỗi thua, ngày/tuần/tháng lỗ.
- Kiểm tra độ ổn định tham số, kết quả từng tháng, và một giai đoạn ngoài mẫu **chưa tham gia chọn phương án**.
- Ghi lại **mọi phương án đã thử**, kể cả phương án bị loại.
- Chỉ kết luận tốt hơn khi so sánh công bằng. Chỉ số này tốt hơn mà chỉ số khác xấu đi thì báo rõ mức đánh đổi.

### 10.4 Kiểm tra vận hành (báo riêng với kết quả chiến lược)
Các tình huống cần kiểm tra:
- mất kết nối;
- khởi động lại EA/terminal;
- lệnh trùng;
- broker phản hồi chưa rõ (timeout, requote);
- nạp/rút tiền;
- spread giãn, trượt giá;
- lot tối thiểu.

Trước khi người dùng chạy: bảng so sánh với EA gốc, kèm danh sách các bước **đã** và **chưa** kiểm chứng. Không cam kết live giống backtest; phải đo và giải thích sai khác.
