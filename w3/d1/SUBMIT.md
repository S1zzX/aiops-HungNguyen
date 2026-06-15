# W3-D1 Submission — Em

## 3 thứ em học được

1. **Burn rate normalize SLO** — Trước đây em nghĩ chỉ cần alert khi error rate > X%. Nhưng "5% error" có nghĩa rất khác nhau: với SLO 99% thì burn rate = 5 (vừa phải), với SLO 99.9% thì burn rate = 50 (cần page ngay). Burn rate là ngôn ngữ chung cho mọi service bất kể SLO target.

2. **MWMBR giải quyết trade-off window** — Single window không có điểm tối ưu: ngắn thì noisy, dài thì chậm recover. AND của long + short window cho cả hai lợi ích: long window xác nhận sự cố đủ nghiêm trọng, short window đảm bảo alert tắt nhanh khi hết sự cố. Validation cho thấy FP giảm từ 19 xuống 0 mà không mất một TP nào.

3. **SLI phải proportional với user pain** — CPU và memory không làm SLI được vì không proportional: CPU 80% nhưng user vẫn OK, hoặc CPU 20% nhưng deadlock khiến user timeout. Chỉ những metric đo trực tiếp "user có dùng được service không" (5xx rate, latency p99, js_error rate) mới là SLI hợp lệ.

## 1 thứ vẫn chưa rõ

Khi nào nên tách latency SLO riêng khỏi availability SLO? Trong lab này em gộp latency vào formula của availability SLI (count request where status OK AND latency < 500ms), nhưng Google SRE Workbook nói có thể tách thành 2 SLO độc lập. Trade-off giữa 1 SLI đơn giản vs 2 SLI riêng biệt trong production là gì?

## 1 trade-off trong SLO decision của em mà em không chắc

Chọn SLO 99.0% cho api trong khi baseline hiện tại chỉ là 97.63% — tức là hệ thống đang miss SLO ngay từ ngày đầu. Lý do chọn: tạo áp lực cải thiện rõ ràng. Nhưng trade-off là team sẽ liên tục thấy "SLO missed" trong dashboard ngay cả tuần không có incident nghiêm trọng, có thể gây alert fatigue hoặc mất niềm tin vào SLO. Có thể nên đặt SLO = 97% trước (match baseline), ratchet lên 99% sau 1 quý cải thiện — nhưng em chưa chắc approach nào tốt hơn trong production thực.

## Validation report

- noise_reduction_pct: **86.4%**
- mttd_delta_s: **0s**
- false_negative: **0**
- verdict: **pass**
