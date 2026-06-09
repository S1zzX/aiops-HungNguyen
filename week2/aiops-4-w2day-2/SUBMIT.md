# W2-D2 Submit

## Câu 1: Confidence và threshold auto-rollback

Confidence của top-1 trong cluster `c-000-000` là **0.9955**.

Nếu phải set threshold để auto-rollback mà không cần SRE xác nhận, tôi chọn **0.85**. Lý do: 0.9955 gần bằng 1.0 — trong thực tế điều này có nghĩa khoảng cách giữa candidate #1 và #2 rất lớn, đây là tín hiệu tốt. Nhưng rollback là một write action có tác động production nên tôi muốn threshold đủ thấp để bắt được real incident (0.85 cover case này) nhưng đủ cao để loại bỏ các cluster mơ hồ khi top-2 score sát nhau. Không nên đặt trên 0.90 vì sẽ bỏ sót các incident mà graph scoring có chút nhiễu do thiếu edge trong service map.

## Câu 2: Variant classifier đã chọn — A (rule-based / kNN top-1 từ retrieval)

Tôi dùng **variant A**: lấy `root_cause_class` và `remediation` từ incident top-1 trả về bởi keyword retrieval (kNN-style, k=1). Thực tế trả về `connection_pool_exhaustion` từ INC-2025-11-08, kết quả đúng.

**Trade-off so với variant C (paid LLM)**:
- Rule-based deterministic và miễn phí — cùng input luôn cho cùng output, dễ debug và audit
- Rule-based thất bại khi incident hoàn toàn mới, không có match gần trong history (ví dụ: lần đầu gặp `tls_expiry` trên service mới)
- LLM xử lý tốt hơn với case mới và có thể tổng hợp từ nhiều incident tương tự thay vì chỉ copy top-1
- Với GeekShop hiện tại (30 incident lịch sử), kNN top-1 là đủ. Khi history tăng lên 300+ incident, TF-IDF hoặc embedding-based retrieval sẽ vượt trội hơn keyword overlap đơn giản.

## Câu 3: Industry landscape — pipeline gần product nào nhất

Pipeline tôi xây gần nhất với **Dynatrace Davis** — nó giả định service graph đáng tin cậy (tương đương Smartscape = `services.json` của chúng ta) và dùng topology + temporal signal để rank candidate một cách deterministic, trả về top-1 root cause nhanh.

Với GeekShop (e-commerce, alert volume trung bình-cao, service map tương đối ổn định với ~10 service), lựa chọn này hợp lý vì:
- Service graph ít thay đổi — service mới được thêm vào không thường xuyên
- Pattern cascade (payment → checkout → edge-lb) có thể dự đoán và graph traversal xử lý tốt
- Tốc độ phản hồi quan trọng trong incident; graph-only cho kết quả dưới 1 giây

Trường hợp nên đổi là nếu GeekShop chuyển sang kiến trúc microservices nặng hoặc serverless, khi graph trở nên không đáng tin. Khi đó **Causely** (causal inference không cần giả định topology) sẽ phù hợp hơn. Hiện tại, graph-first là lựa chọn đúng cho domain này.
