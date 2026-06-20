# W3-D3 Submission — HungNguyen

## Outage chosen
- **ID:** 3
- **Tên:** Cloudflare WAF Regex (2019-07-02)
- **Lý do em chọn cái này:** Em muốn làm việc với một failure mode thuần code/data-dependent
  thay vì infrastructure-dependent — một regex pattern trông hoàn toàn bình thường trên
  phần lớn input nhưng lại chạy theo thời gian lũy thừa trên input adversarial. Đây là
  loại bug dễ lọt qua review nhất và em có thể reproduce rẻ tiền ngay trên máy local,
  không cần dựng multi-node network như case GitHub split-brain.
- **Failure mode:** Catastrophic backtracking (regex), kết hợp với global atomic deploy
  không có canary buffer — một mình cái regex đủ tệ, nhưng deploy toàn bộ edge cùng lúc
  mới biến nó thành outage toàn cầu.

---

## 3 thứ em học từ outage này

1. **Catastrophic backtracking là thật, không phải lý thuyết.** Em đo trực tiếp trên regex
   của pack (`(?:(?:"|\d|.*)+(?:.*=.*))`): input 26 ký tự `x` không có dấu `=` cuối làm
   request mất **9,834ms** (gần 10 giây), trong khi baseline bình thường chỉ ~280ms — tức
   là tăng **35 lần**. Chạy thêm lần nữa vẫn cho 10,039ms. Cái số này không phải benchmark
   trên paper, nó là output thực từ container em vừa chạy.

2. **RCA "đúng" theo logic nhưng vẫn có thể sai theo nghĩa thực tế.** Pipeline của em trả
   về `root_service=api-gateway, confidence=0.7` — topology-aware, trace từ `frontend` lên
   đúng service. Nhưng `api-gateway` là service *đang chạy* WAF middleware, không phải node
   "WAF rule" hay "regex version" trong topology graph. Người on-call nhận được kết quả này
   vẫn phải tự đi kiểm tra xem gần đây `api-gateway` có deploy gì không. Pipeline đúng ở
   mức abstraction nó có, nhưng mức đó không đủ fine-grained cho incident class này.

3. **Detection của em chỉ hoạt động vì em tự viết prober.** Pipeline không có khả năng tự
   phát hiện endpoint nào đó "từng nhanh giờ chậm" — nó hoàn toàn reactive, chỉ xử lý
   alert được push vào `/ingest`. Nếu không có ai viết và chạy prober bên ngoài cho route
   đó, outage này chạy bao lâu cũng không có alert nào cả. Đây là gap lớn hơn em tưởng
   trước khi làm bài.

---

## 1 thứ pipeline của em sẽ vẫn miss nếu outage này xảy ra real

- **Pattern:** Bất kỳ silent-CPU-pin failure nào trên route mà chưa có ai đăng ký synthetic
  prober.
- **Tại sao miss:** Hai con đường detection duy nhất của pipeline là (a) có gì đó gọi
  `/ingest` một cách tường minh, hoặc (b) Prometheus query trả về dữ liệu — mà cả hai đều
  không tự kích hoạt chỉ vì một route trở nên chậm. Không có logic nào bên trong pipeline
  kiểm tra "endpoint này trước đây response 280ms, bây giờ đang response 10,000ms."
- **Mitigation idea:** ADR-001 — đưa synthetic latency probing vào trong pipeline như một
  first-class component, so sánh với rolling baseline và tự gọi `/ingest` khi phát hiện
  regression, thay vì phụ thuộc vào prober bên ngoài mà có thể có hoặc không có tùy route.

---

## 1 quyết định trong ADR mà em không hoàn toàn chắc

Trong ADR-001, em quyết định dùng "latency vượt baseline một bội số nhất định" làm trigger,
nhưng em chưa chốt được bội số đó là bao nhiêu và baseline nên tính thế nào — rolling
average? p50? p99 của N phút gần nhất? Nếu threshold quá chặt thì false-positive trên
traffic variance bình thường; quá lỏng thì bắt regression chậm. Em nghĩ cần tune per-endpoint
thay vì dùng global value, nhưng em chưa có data traffic thực để validate con số cụ thể nào.
Đây là phần em muốn honest là chưa chắc, không muốn hardcode một số mà chưa đo được.

---

## Cost model verdict cho stack của em

| Chỉ số | Giá trị |
|--------|---------|
| ROI | **2.0** |
| Payback | **0.5 tháng** |
| Verdict | **worth_it** |

Inputs em dùng cho Scenario 3 (mid-tier e-commerce checkout, 60 services):
- 4 incidents/tháng × 1.5h avg × $15,000/h downtime × 40% MTTR reduction = $36,000 monthly value
- AIOps cost: $18,000/tháng
- ROI = 36,000 / 18,000 = 2.0 → `worth_it`

Em chọn $15,000/h vì stack này nằm trong band "E-commerce mid-tier" ($5k–$50k/h theo §8.2),
không nhỏ như hobby project nhưng cũng chưa đến Amazon-scale — anchor ở giữa-trên của band
là hợp lý nhất với quy mô 60 services.
