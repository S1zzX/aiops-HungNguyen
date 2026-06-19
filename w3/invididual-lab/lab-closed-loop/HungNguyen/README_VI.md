# Hướng Dẫn Chi Tiết Lab: Closed-Loop Auto-Remediation

Bài lab này yêu cầu chúng ta xây dựng một hệ thống **AIOps Orchestrator** tự động (Closed-Loop). Mục tiêu là khi có cảnh báo (Alert) từ hệ thống giám sát, Orchestrator sẽ tự động phát hiện, quyết định cách xử lý, kiểm tra an toàn (Blast-Radius), thực thi kịch bản sửa lỗi (Runbook), và cuối cùng là xác minh xem lỗi đã được khắc phục chưa. Nếu chưa, nó sẽ tự động Rollback (quay xe).

Dưới đây là tài liệu giải thích chi tiết cấu trúc các file đã code và hướng dẫn cách chạy lab từ A-Z.

---

## Phần 1: Giải Thích Các File Trong Thư Mục Giải Bài

Toàn bộ mã nguồn giải bài lab nằm trong thư mục `HungNguyen/`. Dưới đây là chức năng của từng file:

### 1. File điều phối chính & Cấu hình
- **`closed_loop.py`**: Đây là bộ não của toàn bộ hệ thống (Orchestrator). Nó chạy một vòng lặp liên tục mỗi 15 giây để gọi API của Alertmanager lấy danh sách các cảnh báo (alerts) đang kích hoạt. Khi có alert, nó sẽ tạo ra một Thread (luồng) riêng để xử lý cảnh báo đó theo quy trình 5 bước: **Detect → Decide → Act → Verify → Rollback**. Nó cũng tích hợp các cơ chế bảo vệ an toàn để tránh việc hệ thống tự động làm hỏng thêm mọi thứ.
- **`config.yaml`**: File cấu hình chứa các tham số quan trọng:
  - `runbook_map`: Bản đồ quy định lỗi nào thì chạy script nào (Ví dụ: `HighLatency` -> `restart_service.sh`).
  - `runbook_registry`: Danh sách các script được phép chạy (cơ chế chống "ảo giác" LLM Hallucination - chặn việc tự chế ra script không tồn tại).
  - `blast_radius`: Giới hạn an toàn (VD: tối đa 3 hành động mỗi phút, tối đa 5 lần khởi động lại cho mỗi dịch vụ trong 1 giờ).

### 2. Thư mục `engine/` (Các module lõi)
Thư mục này chứa các thành phần hỗ trợ cho `closed_loop.py`:
- **`safety.py`**: Trái tim của sự an toàn, gồm 3 lớp chính:
  - `BlastRadiusGuard`: Đảm bảo hệ thống tự động không vượt quá ngân sách sửa lỗi (ngăn chặn hiệu ứng domino làm sập cả hệ thống).
  - `CircuitBreaker` (Cầu dao): Nếu một dịch vụ tự động sửa lỗi và thất bại 3 lần liên tiếp, cầu dao sẽ "nhảy" (mở), tạm dừng tự động hóa trên dịch vụ đó và yêu cầu con người can thiệp.
  - `ServiceMutex`: Khóa độc quyền (Lock) đảm bảo rằng tại một thời điểm, chỉ có 1 hành động được thực hiện trên 1 dịch vụ (tránh việc 2 cảnh báo cùng lúc gọi 2 lệnh restart đè lên nhau).
- **`verify.py`**: Chịu trách nhiệm đi hỏi Prometheus xem dịch vụ đã thực sự sống lại chưa sau khi chạy Runbook. Nó sẽ truy vấn các chỉ số như `up`, `latency_p99_ms`, và `error_rate_pct`. Phải đạt chuẩn 3 lần liên tiếp trong vòng 60 giây thì mới tính là thành công (VERIFY_PASS).
- **`logger.py`**: Định dạng log chuẩn JSON giúp dễ dàng parse dữ liệu và audit. Mỗi log đều có `event_type` rõ ràng.
- **`metrics.py`**: Chạy một server nhỏ ở cổng `9100` để phơi xuất các số liệu (Prometheus metrics) về chính bản thân Orchestrator (có bao nhiêu alert đã xử lý, bao nhiêu hành động thành công, v.v.).

### 3. Thư mục `runbooks/` (Các kịch bản thực thi)
Chứa các script Bash để tương tác với Docker khi có lỗi:
- **`restart_service.sh`**: Khởi động lại một container.
- **`clear_cache.sh`**: Xóa cache của dịch vụ (có thể gọi lại script restart).
- **`scale_replicas.sh`**: Tăng số lượng replicas (chỉ in ra log ảo trong môi trường docker-compose này).
- **`multi_step_deploy.sh`**: Script đặc biệt cho Scenario 4, mô phỏng quá trình deploy qua 3 bước (A, B, C) và có khả năng rollback ngược từng bước (rollback-B, rollback-A) nếu bước C thất bại. Đặc biệt, script nào cũng hỗ trợ cờ `--dry-run` để chạy thử (chỉ in ra chứ không làm thật).

### 4. Tài liệu báo cáo
- **`DESIGN.md`**: File trả lời 4 câu hỏi thiết kế kiến trúc bắt buộc của lab (Tại sao dùng Rule-based? Tại sao đặt Blast Radius là số đó? Tại sao verify 60s? Tại sao cầu dao cần reset thủ công?).
- **`SUBMIT.md`**: Bản ghi chép (log) chứng minh hệ thống đã xử lý thành công 6 kịch bản lỗi (Chaos Scenarios) mà đề bài yêu cầu.

---

## Phần 2: Hướng Dẫn Chạy Lab Từ A-Z

Để tự mình chạy và kiểm chứng lại toàn bộ lab, bạn hãy làm theo các bước sau trong môi trường WSL / Linux:

### Bước 1: Khởi động hệ thống (Stack)
Đầu tiên, bật tất cả các dịch vụ (FastAPI, Prometheus, Alertmanager, Grafana, v.v.) bằng Docker Compose:
```bash
cd data-pack
bash scripts/start_stack.sh
```
*Đợi khoảng 30s cho script kiểm tra Health Check thông báo "All services up".*

### Bước 2: Chạy Orchestrator (Closed-Loop)
Mở một Terminal/Tab mới, kích hoạt môi trường ảo (virtual environment) chứa Python và chạy file `closed_loop.py`:
```bash
source venv/bin/activate
cd HungNguyen
python closed_loop.py --config config.yaml
```
*Orchestrator sẽ chạy liên tục trên terminal này và in ra các log JSON. Nó đang lắng nghe Alertmanager.*

### Bước 3: Tạo lỗi giả lập (Chaos Scenarios)
Mở một Terminal/Tab thứ ba để tiêm lỗi vào hệ thống.

**Kịch bản 1: Lỗi độ trễ cao (High Latency)**
Vì Docker Desktop trên WSL đôi khi không hỗ trợ `tc/nsenter` để giả lập mạng chậm, cách tốt nhất là gửi thẳng một cảnh báo giả tới Alertmanager bằng lệnh `curl`:
```bash
curl -s -X POST http://localhost:9093/api/v2/alerts \
-H 'Content-Type: application/json' \
-d '[{"labels":{"alertname":"HighLatency","service":"payment-svc","severity":"critical"}}]'
```
*Sang màn hình Terminal của Orchestrator (ở Bước 2), bạn sẽ thấy log chạy: ALERT_DETECTED -> DECIDE_RUNBOOK -> DRY_RUN_PASS -> ACTION_EXECUTED -> VERIFY.*

**Kịch bản 2 & 3: Dịch vụ bị chết (Instance Down) & Cầu Dao (Circuit Breaker)**
Giả lập dịch vụ `checkout-svc` bị chết:
```bash
docker stop ronki-checkout-svc
```
*Chờ khoảng 15s để Prometheus nhận ra và Alertmanager bắn cảnh báo. Orchestrator sẽ chạy script restart. Tuy nhiên, nếu bạn cứ liên tục làm chết dịch vụ này 3 lần (mỗi lần orchestrator tự cứu sống nó, bạn lại tắt nó đi), Orchestrator sẽ bật Cầu Dao (`CIRCUIT_BREAKER_HALT`) và từ chối cứu nó nữa.*

**Kịch bản 5: Hai cảnh báo cùng lúc (Concurrent Alert Race)**
```bash
bash data-pack/scripts/inject_fault.sh --concurrent ronki-payment-svc ronki-inventory-svc
```
*Orchestrator sẽ xử lý song song cả 2 dịch vụ cùng lúc nhờ cơ chế Multi-threading.*

### Bước 4: Kiểm tra Dashboard
Bạn có thể mở các Dashboard sau trên trình duyệt để quan sát hệ thống:
- **Prometheus**: `http://localhost:9090`
- **Alertmanager**: `http://localhost:9093`
- **Orchestrator Metrics**: `http://localhost:9100/metrics`

### Bước 5: Dọn dẹp (Tear Down)
Sau khi test xong toàn bộ, tắt Orchestrator bằng cách ấn `Ctrl+C` ở Terminal Bước 2.
Tắt toàn bộ container để giải phóng RAM:
```bash
cd data-pack
docker-compose -f configs/docker-compose.yml down
```

Chúc bạn bảo vệ lab thành công! Mọi tài liệu và code đều đã sẵn sàng để giáo viên chấm.
