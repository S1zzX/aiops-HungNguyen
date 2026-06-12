# 📓 Notebook: Evidence-Driven Remediation Engine
> Giải thích chi tiết từng hàm, dữ liệu, và cách hoạt động — kèm ví dụ thực tế

---

## 📁 MỤC LỤC

1. [Tổng quan kiến trúc](#tổng-quan-kiến-trúc)
2. [Dữ liệu: audit.jsonl là gì?](#dữ-liệu-auditjsonl-là-gì)
3. [Dữ liệu: incidents_history.json là gì?](#dữ-liệu-incidents_historyjson-là-gì)
4. [Dữ liệu: topology.json là gì?](#dữ-liệu-topologyjson-là-gì)
5. [features.py — Layer 1: Trích xuất đặc trưng](#featurespy--layer-1-trích-xuất-đặc-trưng)
6. [retrieval.py — Layer 2: Tìm tiền lệ & voting](#retrievalpy--layer-2-tìm-tiền-lệ--voting)
7. [decision.py — Layer 3: Chọn hành động](#decisionpy--layer-3-chọn-hành-động)
8. [engine.py — Entry point](#enginepy--entry-point)
9. [optional-helpers.py — Helpers phụ](#optional-helperspy--helpers-phụ)
10. [grade.py — Chấm điểm tự động](#gradepy--chấm-điểm-tự-động)

---

## Tổng quan kiến trúc

```
Incident JSON (E01.json)
        │
        ▼
┌─────────────────┐
│  Layer 1        │  features.py
│  Feature        │  → Biến 500 dòng log thành vector gọn
│  Extraction     │
└────────┬────────┘
         │ incident_vector
         ▼
┌─────────────────┐
│  Layer 2        │  retrieval.py
│  Retrieval +    │  → Tìm 5 incident tương tự trong lịch sử
│  Voting         │  → Vote action tốt nhất
└────────┬────────┘
         │ candidates + neighbors
         ▼
┌─────────────────┐
│  Layer 3        │  decision.py
│  Decision       │  → Tính EV, kiểm tra blast gate
│  Making         │  → Chọn action cuối cùng
└────────┬────────┘
         │
         ▼
    audit.jsonl  ←  ghi kết quả ra đây
```

**Luồng hoạt động thực tế:**
- Input: `eval/E01.json` (incident đang xảy ra)
- Output: Quyết định như `increase_pool_size` hay `page_oncall`
- Tất cả quyết định được ghi vào `audit.jsonl` để chấm điểm

---

## Dữ liệu: audit.jsonl là gì?

`audit.jsonl` là **file nhật ký kết quả** — mỗi dòng là một JSON object chứa quyết định của engine cho 1 incident.

### Cấu trúc 1 dòng trong audit.jsonl

```json
{
  "incident_id": "E01",
  "selected_action": "increase_pool_size",
  "params": {
    "service": "payment-svc",
    "from_value": 50,
    "to_value": 100
  },
  "confidence": 0.671,
  "consensus_score": 0.545,
  "top_3_neighbors": [...],
  "blast_radius_check": {
    "blast_gate": 3,
    "confidence_gate": 0.5,
    "min_confidence": 0.2
  },
  "evidence": {
    "reason": "Top EV=0.5826: p_success=1.00, blast=1, similarity=0.545",
    "ev_table": [...],
    "is_ood": false,
    "max_similarity": 0.545,
    "affected_services": ["checkout-svc", "edge-lb", "payment-svc"],
    "top_error_edge": [...],
    "candidates_raw": [...]
  }
}
```

### Giải thích từng trường

| Trường | Ý nghĩa | Ví dụ |
|--------|---------|-------|
| `incident_id` | ID của incident | `"E01"` |
| `selected_action` | Hành động được chọn | `"increase_pool_size"` |
| `params` | Tham số của hành động | `{"service": "payment-svc", "from_value": 50, "to_value": 100}` |
| `confidence` | Độ tin cậy (0.0–1.0) | `0.671` — 67.1% chắc chắn |
| `consensus_score` | Similarity với neighbor gần nhất | `0.545` — 54.5% giống incident cũ nhất |
| `top_3_neighbors` | 3 incident lịch sử gần nhất | Xem bên dưới |
| `blast_radius_check` | Ngưỡng kiểm tra blast radius | `blast_gate=3` |
| `evidence.is_ood` | Có phải incident lạ không? | `false` — đã biết loại này |
| `evidence.ev_table` | Bảng tính EV cho từng action | Xem bên dưới |

### top_3_neighbors — ví dụ thực tế từ E01:

```json
[
  {
    "id": "INC-2025-11-08",
    "similarity": 0.545,         ← giống 54.5%
    "root_cause_class": "connection_pool_exhaustion",
    "outcome": "success",         ← đã fix được
    "actions_taken": [
      "rollback_service:payment-svc:previous",
      "increase_pool_size:payment-svc:50:100"
    ]
  },
  {
    "id": "INC-2025-09-05",
    "similarity": 0.4075,        ← giống 40.75%
    "outcome": "success",
    ...
  },
  {
    "id": "INC-2026-05-10",
    "similarity": 0.4075,
    "outcome": "partial",        ← chỉ fix được một phần
    ...
  }
]
```

### ev_table — bảng EV cho E01:

```json
[
  {
    "action": "increase_pool_size",
    "p_success": 1.0,      ← 100% thành công trong lịch sử
    "vote_weight": 0.4631, ← tổng trọng số vote
    "vote_frac": 0.452,    ← chiếm 45.2% tổng vote
    "confidence": 0.671,
    "ev": 0.5826,          ← Expected Value cao nhất → WIN
    "blast": 1,            ← chỉ ảnh hưởng 1 service
    "cost": 1              ← cost thấp nhất
  },
  {
    "action": "rollback_service",
    "p_success": 0.667,
    "ev": 0.4168           ← EV thấp hơn → không được chọn
  }
]
```

---

## Dữ liệu: incidents_history.json là gì?

File này chứa **~29 incident đã xảy ra trong quá khứ**, đã biết nguyên nhân và kết quả. Engine dùng đây như "bộ nhớ kinh nghiệm".

### Cấu trúc 1 entry trong history:

```json
{
  "id": "INC-2025-11-08",
  "root_cause_class": "connection_pool_exhaustion",
  "affected_services": ["payment-svc", "payments-db", "checkout-svc"],
  "log_signatures": [
    "ConnectionPool: timeout acquiring connection",
    "Failed to forward request: pool exhausted"
  ],
  "trace_signatures": [
    {
      "from": "checkout-svc",
      "to": "payment-svc",
      "p99_deviation_ratio": 2.4,
      "error_rate": 0.18
    }
  ],
  "metric_signatures": [
    {
      "service": "payment-svc",
      "metric": "conn_pool_used",
      "delta": "30 -> 95"      ← từ 30 lên 95 connections
    }
  ],
  "actions_taken": [
    "rollback_service:payment-svc:previous",
    "increase_pool_size:payment-svc:50:100"
  ],
  "outcome": "success",
  "mttr_minutes": 19           ← fix xong sau 19 phút
}
```

### Phân loại 29 incidents theo root_cause_class:

| Root Cause | Số lần | Outcome phổ biến |
|-----------|--------|-----------------|
| `connection_pool_exhaustion` | 3 | success |
| `slow_query` | 3 | success/partial |
| `bad_deploy` | 1 | partial |
| `lock_contention` | 1 | success |
| `model_drift` | 1 | success |
| `tls_expiry` | 1 | success |
| `config_push` | 1 | success |
| `batch_overlap` | 1 | partial |
| ... | ... | ... |

### Tại sao chỉ có ~29 entries quan trọng?

Với dataset nhỏ như vậy:
- TF-IDF không hoạt động tốt (IDF không ổn định)
- **Jaccard similarity** là lựa chọn đúng (xem giải thích ở retrieval.py)
- Mỗi incident mới phải "học" từ rất ít tiền lệ

---

## Dữ liệu: topology.json là gì?

File này mô tả **sơ đồ kiến trúc microservices** — service nào gọi service nào.

### Cấu trúc:

```json
{
  "nodes": [
    { "id": "edge-lb",      "tier": "edge"  },
    { "id": "auth-svc",     "tier": "api"   },
    { "id": "checkout-svc", "tier": "api"   },
    { "id": "payment-svc",  "tier": "api"   },
    { "id": "payments-db",  "tier": "store" }
  ],
  "edges": [
    { "from": "edge-lb",      "to": "checkout-svc", "protocol": "http"     },
    { "from": "checkout-svc", "to": "payment-svc",  "protocol": "http"     },
    { "from": "payment-svc",  "to": "payments-db",  "protocol": "postgres" }
  ]
}
```

### Sơ đồ kiến trúc:

```
Internet
   │
   ▼
[edge-lb] ──────────────────────────┐
   │                                │
   ├──► [auth-svc]                  │
   ├──► [catalog-svc] ──► [catalog-db]
   ├──► [search-svc]  ──► [catalog-db]
   └──► [checkout-svc]
            │
            ├──► [payment-svc]  ──► [payments-db]
            ├──► [cart-svc]     ──► [cart-redis]
            │         └──► [catalog-svc]
            ├──► [inventory-svc] ──► [catalog-db]
            └──► [notification-svc] ──► [kafka-events]

[recommender-svc] ◄── [catalog-svc]
[recommender-svc] ──► [catalog-db]
```

### Tier giải thích:
| Tier | Nghĩa | Services |
|------|-------|---------|
| `edge` | Cổng vào từ internet | edge-lb |
| `api` | Business logic | checkout-svc, payment-svc, ... |
| `ml` | Machine learning | recommender-svc |
| `store` | Database/Cache | payments-db, catalog-db, cart-redis, kafka-events |

### topology.json được dùng ở đâu?

Hiện tại topology.json **không được dùng trực tiếp** trong code engine (features.py/retrieval.py/decision.py). Nó là **tài liệu tham khảo** để hiểu quan hệ service → hỗ trợ phân tích thủ công.

---

## features.py — Layer 1: Trích xuất đặc trưng

### Mục đích tổng quát

Chuyển đổi raw incident JSON (500+ dòng log, 80 trace records) thành một **vector nhỏ gọn có thể so sánh** được với lịch sử.

---

### Hàm 1: `_drain_template(msg: str) -> str`

**Mục đích:** Chuẩn hoá 1 dòng log bằng cách thay thế token biến đổi bằng placeholder.

**Vì sao cần?** Hai dòng log cùng nguyên nhân nhưng khác số liệu cụ thể sẽ được map về cùng 1 template.

```python
def _drain_template(msg: str) -> str:
    s = _UUID.sub('<UUID>', msg)     # thay UUID
    s = _IP.sub('<IP>', s)           # thay IP address
    s = _VERSION.sub('<VER>', s)     # thay version như v1.2.3
    s = _HEX.sub('<HEX>', s)         # thay hex string
    s = _PATH.sub('<PATH>', s)       # thay file path
    s = _NUMBER.sub('<NUM>', s)      # thay số
    s = re.sub(r'\s+', ' ', s).strip()
    return s
```

**Ví dụ thực tế:**

| Input (log thô) | Output (template) |
|-----------------|-------------------|
| `"ConnectionPool: timeout waiting 5000ms for host 10.0.1.5"` | `"ConnectionPool: timeout waiting <NUM> for host <IP>"` |
| `"DB query took 8241ms on table orders"` | `"DB query took <NUM> on table orders"` |
| `"Deploy v2.3.1 failed at /app/config"` | `"Deploy <VER> failed at <PATH>"` |
| `"Request a3f7c8d2-... failed"` | `"Request <UUID> failed"` |

**Regex patterns được dùng:**
```python
_NUMBER  = re.compile(r'\b\d+(\.\d+)?(ms|MB|GB|s|%)?\b')  # số + đơn vị
_HEX     = re.compile(r'\b[0-9a-fA-F]{6,}\b')              # hex >= 6 ký tự
_UUID    = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-...')      # UUID format
_PATH    = re.compile(r'(/[\w./\-]+)')                      # /path/to/file
_VERSION = re.compile(r'\bv\d+\.\d+[\.\d]*\b')             # v1.2.3
_IP      = re.compile(r'\b\d{1,3}(\.\d{1,3}){3}\b')        # 192.168.1.1
```

---

### Hàm 2: `extract_log_templates(logs: list[dict]) -> dict[str, int]`

**Mục đích:** Gom 500 dòng log → dict `{template: count}` — tóm tắt "xảy ra chuyện gì".

```python
def extract_log_templates(logs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for entry in logs:
        tmpl = _drain_template(entry.get('msg', ''))
        counts[tmpl] += 1
    return dict(counts)
```

**Ví dụ input (logs thô từ E01):**
```python
logs = [
    {"svc": "payment-svc", "level": "ERROR", "msg": "ConnectionPool: timeout waiting 5000ms"},
    {"svc": "payment-svc", "level": "ERROR", "msg": "ConnectionPool: timeout waiting 3200ms"},
    {"svc": "payment-svc", "level": "ERROR", "msg": "Failed to forward request: pool exhausted"},
    {"svc": "checkout-svc", "level": "WARN",  "msg": "Retry attempt 3 of 5"},
    {"svc": "checkout-svc", "level": "WARN",  "msg": "Retry attempt 4 of 5"},
]
```

**Output:**
```python
{
  "ConnectionPool: timeout waiting <NUM>": 2,   ← xuất hiện 2 lần
  "Failed to forward request: pool exhausted": 1,
  "Retry attempt <NUM> of <NUM>": 2
}
```

**Tác dụng thực tế:** 500 dòng log → chỉ còn 3-5 template đặc trưng để so sánh.

---

### Hàm 3: `extract_trace_features(traces, metrics_window) -> dict`

**Mục đích:** Tính chỉ số lỗi cho từng cặp (from → to) service từ trace data.

```python
def extract_trace_features(traces: list[dict], metrics_window: dict) -> dict[str, dict]:
    edge_feats = {}
    for t in traces:
        key = f"{t['from']}->{t['to']}"
        count = t.get('count', 1) or 1
        err_rate = t.get('error_count', 0) / count
        p50 = t.get('p50_ms', 1) or 1
        p99 = t.get('p99_ms', p50)
        edge_feats[key] = {
            'error_rate': round(err_rate, 4),
            'p99_deviation_ratio': round(p99 / p50, 2),  ← p99/p50: bao nhiêu lần xấu hơn trung bình
            'p99_ms': p99,
            'from': t['from'],
            'to': t['to'],
        }
    return edge_feats
```

**Ví dụ input:**
```python
traces = [
    {
        "from": "checkout-svc",
        "to": "payment-svc",
        "count": 1000,
        "error_count": 620,    ← 620/1000 = 62% lỗi!
        "p50_ms": 280,
        "p99_ms": 2410         ← p99 gấp 8.6x p50
    }
]
```

**Output:**
```python
{
  "checkout-svc->payment-svc": {
    "error_rate": 0.62,            ← 62% requests bị lỗi
    "p99_deviation_ratio": 8.61,   ← p99 cao gấp 8.6x bình thường → rất bất thường
    "p99_ms": 2410,
    "from": "checkout-svc",
    "to": "payment-svc"
  }
}
```

**Cách đọc `p99_deviation_ratio`:**
- `= 1.0` → p99 bằng p50 → hoàn toàn bình thường
- `= 2.0` → p99 gấp đôi p50 → hơi chậm
- `= 8.6` → p99 gấp 8.6 lần p50 → **rất bất thường, có vấn đề nghiêm trọng**

---

### Hàm 4: `extract_affected_services(incident: dict) -> list[str]`

**Mục đích:** Xác định danh sách service nào đang bị ảnh hưởng.

```python
def extract_affected_services(incident: dict) -> list[str]:
    services = set()

    # Nguồn 1: alert chỉ định service nào
    alert_svc = incident.get('trigger_alert', {}).get('service')
    if alert_svc:
        services.add(alert_svc)

    # Nguồn 2: trace có error_rate > 10%
    for t in incident.get('traces', []):
        count = t.get('count', 1) or 1
        err_rate = t.get('error_count', 0) / count
        if err_rate > 0.1:
            services.add(t['from'])
            services.add(t['to'])

    # Nguồn 3: log có level ERROR/CRITICAL
    for log in incident.get('logs', []):
        if log.get('level') in ('ERROR', 'CRITICAL'):
            svc = log.get('svc')
            if svc:
                services.add(svc)

    return sorted(services)
```

**Ví dụ cho E01:**
```python
incident = {
    "trigger_alert": {"service": "payment-svc"},
    "traces": [
        {"from": "checkout-svc", "to": "payment-svc", "count": 100, "error_count": 62}
        # error_rate = 0.62 > 0.1 → thêm cả checkout-svc và payment-svc
    ],
    "logs": [
        {"svc": "edge-lb", "level": "WARN", "msg": "..."},
        # edge-lb chỉ WARN, không ERROR → không thêm vào
    ]
}

# Output:
["checkout-svc", "edge-lb", "payment-svc"]
# ↑ edge-lb vào từ trigger_alert hoặc logs có ERROR
```

**Tại sao union 3 nguồn?** Vì:
- Alert chỉ đặt ở 1 service (thường là nơi đặt threshold)
- Nhưng nguyên nhân thực có thể là service upstream hay downstream
- Log ERROR giúp bắt service không có trace nhưng vẫn bị ảnh hưởng

---

### Hàm 5: `extract_features(incident: dict) -> dict`

**Mục đích:** Hàm tổng hợp — gọi tất cả hàm trên và trả về 1 `incident_vector` hoàn chỉnh.

```python
def extract_features(incident: dict) -> dict[str, Any]:
    logs = incident.get('logs', [])
    traces = incident.get('traces', [])
    metrics = incident.get('metrics_window', {})

    log_templates = extract_log_templates(logs)
    trace_feats = extract_trace_features(traces, metrics)
    affected = extract_affected_services(incident)

    # Tìm edge có error_rate cao nhất → "đường nóng"
    sorted_edges = sorted(
        trace_feats.items(),
        key=lambda x: (x[1]['error_rate'], x[1]['p99_deviation_ratio']),
        reverse=True
    )
    top_error_edge = sorted_edges[0] if sorted_edges else None

    return {
        'incident_id': incident.get('incident_id', ''),
        'alert_service': ...,
        'severity': ...,
        'log_templates': log_templates,
        'trace_features': trace_feats,
        'affected_services': affected,
        'top_error_edge': top_error_edge,
        'log_template_set': set(log_templates.keys()),
        'top_error_trace_service': top_error_edge[1]['to'] if top_error_edge else None,
    }
```

**Output hoàn chỉnh cho E01:**
```python
{
  "incident_id": "E01",
  "alert_service": "payment-svc",
  "severity": "critical",
  "log_templates": {
    "ConnectionPool: timeout waiting <NUM>": 45,
    "Failed to forward request: pool exhausted": 12,
  },
  "trace_features": {
    "checkout-svc->payment-svc": {
      "error_rate": 0.62,
      "p99_deviation_ratio": 8.61,
      "p99_ms": 2410
    }
  },
  "affected_services": ["checkout-svc", "edge-lb", "payment-svc"],
  "top_error_edge": ("checkout-svc->payment-svc", {...}),
  "log_template_set": {"ConnectionPool: ...", "Failed to forward ..."},
  "top_error_trace_service": "payment-svc"   ← service bị gọi mà lỗi
}
```

---

## retrieval.py — Layer 2: Tìm tiền lệ & Voting

### Mục đích tổng quát

Tìm trong 29 incident lịch sử những cái nào **giống nhất** với incident hiện tại, rồi **vote** xem action nào nên làm.

---

### Hàm 6: `_drain_tokens(s: str) -> set[str]`

**Mục đích:** Tokenise 1 chuỗi text thành tập từ để tính Jaccard.

```python
def _drain_tokens(s: str) -> set[str]:
    s = re.sub(r'[^a-zA-Z0-9 _]', ' ', s)  # chỉ giữ chữ và số
    return {t.lower() for t in s.split() if len(t) > 2}  # bỏ từ quá ngắn
```

**Ví dụ:**
```python
_drain_tokens("ConnectionPool: timeout acquiring connection")
# → {"connectionpool", "timeout", "acquiring", "connection"}

_drain_tokens("Failed to forward request: pool exhausted")
# → {"failed", "forward", "request", "pool", "exhausted"}

_drain_tokens("DB query latency > 5s on table")
# → {"query", "latency", "table"}
# ↑ "DB", "5s", ">", "on" đều bị loại (quá ngắn hoặc ký tự đặc biệt)
```

---

### Hàm 7: `_jaccard(a: set, b: set) -> float`

**Mục đích:** Tính Jaccard similarity giữa 2 tập hợp.

**Công thức:** `|A ∩ B| / |A ∪ B|`

```python
def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0   # 2 tập rỗng → coi như giống nhau
    inter = len(a & b)   # giao
    union = len(a | b)   # hợp
    return inter / union if union else 0.0
```

**Ví dụ:**
```python
# Trường hợp 1: Rất giống nhau
a = {"connectionpool", "timeout", "connection"}
b = {"connectionpool", "timeout", "acquiring", "connection"}
_jaccard(a, b) = 3 / 4 = 0.75

# Trường hợp 2: Không liên quan
a = {"connectionpool", "timeout"}
b = {"query", "latency", "table"}
_jaccard(a, b) = 0 / 5 = 0.0

# Trường hợp 3: Giống một phần
a = {"payment", "svc", "connection", "pool"}
b = {"payment", "svc", "deadlock", "lock"}
_jaccard(a, b) = 2 / 6 = 0.333
```

---

### Hàm 8: `similarity(query_vec: dict, history_entry: dict) -> float`

**Mục đích:** Tính độ giống nhau tổng hợp giữa incident hiện tại và 1 incident trong lịch sử.

**Công thức:**
```
similarity = 0.45 × log_sim + 0.35 × trace_sim + 0.20 × svc_sim
```

```python
def similarity(query_vec: dict, history_entry: dict) -> float:

    # === PHẦN 1: Log similarity (trọng số 45%) ===
    query_log_tokens = set()
    for tmpl in query_vec.get('log_template_set', set()):
        query_log_tokens |= _drain_tokens(tmpl)   # gom tất cả token từ templates

    hist_log_tokens = set()
    for sig in history_entry.get('log_signatures', []):
        hist_log_tokens |= _drain_tokens(sig)

    log_sim = _jaccard(query_log_tokens, hist_log_tokens)

    # === PHẦN 2: Trace similarity (trọng số 35%) ===
    # Lấy edges có error_rate > 10%
    query_edges = {
        (v['from'], v['to'])
        for v in query_trace_feats.values()
        if v.get('error_rate', 0) > 0.1
    }
    query_to_svcs = {v['to'] for v in ... if error > 0.1}  # chỉ destination

    hist_edges = {(t['from'], t['to']) for t in history_entry.get('trace_signatures', [])}
    hist_to_svcs = {t['to'] for t in history_entry.get('trace_signatures', [])}

    trace_edge_sim = _jaccard(query_edges, hist_edges)    # exact edge match
    trace_svc_sim  = _jaccard(query_to_svcs, hist_to_svcs)  # chỉ destination
    trace_sim = 0.5 * trace_edge_sim + 0.5 * trace_svc_sim

    # === PHẦN 3: Service overlap (trọng số 20%) ===
    query_svcs = set(query_vec.get('affected_services', []))
    hist_svcs  = set(history_entry.get('affected_services', []))
    svc_sim = _jaccard(query_svcs, hist_svcs)

    return round(0.45 * log_sim + 0.35 * trace_sim + 0.20 * svc_sim, 4)
```

**Ví dụ chi tiết — E01 vs INC-2025-11-08:**

```python
# Query E01:
query_log_tokens = {"connectionpool", "timeout", "failed", "forward", "pool", "exhausted"}
query_edges = {("checkout-svc", "payment-svc")}
query_svcs = {"checkout-svc", "edge-lb", "payment-svc"}

# History INC-2025-11-08:
hist_log_tokens = {"connectionpool", "timeout", "acquiring", "connection",
                   "failed", "forward", "request", "pool", "exhausted"}
hist_edges = {("checkout-svc", "payment-svc")}
hist_svcs = {"payment-svc", "payments-db", "checkout-svc"}

# Tính:
log_sim   = _jaccard({...6 tokens}, {...9 tokens})
          = 6 chung / 9 tổng = 0.667

trace_edge_sim = _jaccard({(checkout,payment)}, {(checkout,payment)}) = 1.0  ← exact match!
trace_svc_sim  = _jaccard({"payment-svc"}, {"payment-svc"}) = 1.0
trace_sim = 0.5*1.0 + 0.5*1.0 = 1.0

svc_sim = _jaccard({"checkout-svc","edge-lb","payment-svc"},
                   {"payment-svc","payments-db","checkout-svc"})
        = 2 chung / 4 tổng = 0.5

similarity = 0.45*0.667 + 0.35*1.0 + 0.20*0.5
           = 0.300 + 0.350 + 0.100 = 0.75
           # (giá trị thực tế = 0.545 vì token overlap không hoàn toàn như ví dụ này)
```

**Tại sao trọng số 0.45 / 0.35 / 0.20?**
- Log: signal mạnh nhất, phản ánh nguyên nhân gốc
- Trace: signal thứ 2, cho biết đường đi của lỗi
- Service: signal yếu nhất, dễ trùng ngẫu nhiên

---

### Hàm 9: `retrieve_and_vote(query_vec, history, top_k=5) -> dict`

**Mục đích:** Hàm chính của Layer 2 — tìm top-5 neighbors rồi vote action.

```python
def retrieve_and_vote(query_vec, history, top_k=5):
    # Bước 1: Tính similarity với tất cả 29 incidents
    scored = []
    for entry in history:
        sim = similarity(query_vec, entry)
        scored.append((sim, entry))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_k]  # lấy 5 tương tự nhất

    # Bước 2: OOD detection
    max_sim = top[0][0] if top else 0.0
    is_ood = max_sim < OOD_THRESHOLD  # 0.22

    # Bước 3: Voting có trọng số
    action_votes = defaultdict(float)
    for sim, entry in top:
        ow = OUTCOME_WEIGHT[entry['outcome']]  # success=1.0, partial=0.5, failed=0.1
        weight = (sim ** 2) * ow  # BÌNH PHƯƠNG sim → amplify neighbor gần

        for raw_action in entry['actions_taken']:
            parsed = _parse_history_action(raw_action)
            action_votes[parsed['name']] += weight

    # Bước 4: Tính p_success
    candidates = []
    for action_name, vote_weight in sorted(action_votes.items(), ...):
        p_success = successes / total  # tỷ lệ thành công trong history
        candidates.append({...})

    return {
        'candidates': candidates,
        'top_3_neighbors': [...],
        'max_similarity': max_sim,
        'is_ood': is_ood,
    }
```

**Ví dụ voting cho E05 — lý do `increase_pool_size` thắng `rollback_service`:**

```
Neighbors của E05:
  INC-2025-11-08: sim=0.686, outcome=success
    actions: [rollback_service, increase_pool_size]
  INC-2025-09-05: sim=0.686, outcome=success  ← cùng sim (tính approx)
    actions: [rollback_service, increase_pool_size]
  INC-2026-05-10: sim=0.219, outcome=partial  ← sim thấp + partial
    actions: [rollback_service]

Tính vote weight:
  INC-2025-11-08: weight = 0.686² × 1.0 = 0.471
  INC-2025-09-05: weight = 0.686² × 1.0 = 0.471
  INC-2026-05-10: weight = 0.219² × 0.5 = 0.024

rollback_service nhận vote từ: tất cả 3 → 0.471 + 0.471 + 0.024 = 0.966
increase_pool_size nhận vote từ: chỉ 2 success → 0.471 + 0.471 = 0.942

Vote weight gần nhau! Nhưng...
p_success(rollback): chỉ 2/3 neighbors success → 0.667
p_success(increase_pool_size): 2/2 neighbors success → 1.0

→ Trong Layer 3, EV của increase_pool_size sẽ cao hơn vì p_success=1.0
```

**Tại sao bình phương similarity (`sim²`)?**

```
Nếu không bình phương (dùng sim thẳng):
  neighbor_gần (sim=0.60): weight = 0.60
  3 neighbor_xa (sim=0.20): weight = 0.20 × 3 = 0.60 → BẰNG NHAU!

Với bình phương:
  neighbor_gần (sim=0.60): weight = 0.36
  3 neighbor_xa (sim=0.20): weight = 0.04 × 3 = 0.12 → neighbor gần mạnh gấp 3×
```

**OOD detection — ngưỡng 0.22:**

```python
OOD_THRESHOLD = 0.22

# E07: max_similarity = 0.214 < 0.22 → OOD = True → page_oncall
# E08: max_similarity = 0.197 < 0.22 → OOD = True → page_oncall
# E01: max_similarity = 0.545 > 0.22 → OOD = False → auto-act
```

---

## decision.py — Layer 3: Chọn hành động

### Mục đích tổng quát

Nhận danh sách candidates từ Layer 2, tính **Expected Value (EV)** cho từng action, áp dụng các safety gate, chọn action tốt nhất.

---

### Hàm 10: `_action_meta(action_name, catalog) -> dict`

**Mục đích:** Tra cứu metadata của action từ `actions.yaml`.

```python
def _action_meta(action_name: str, catalog: list[dict]) -> dict:
    for a in catalog:
        if a['name'] == action_name:
            return a
    return {}
```

**Ví dụ:**
```python
catalog = [
    {"name": "rollback_service", "cost_min": 10, "blast_radius_services": 1, ...},
    {"name": "increase_pool_size", "cost_min": 1, "blast_radius_services": 1, ...},
    {"name": "network_policy_revert", "cost_min": 15, "blast_radius_services": 4, ...},
]

_action_meta("increase_pool_size", catalog)
# → {"name": "increase_pool_size", "cost_min": 1, "downtime_min": 0,
#    "blast_radius_services": 1, "rollback_window_sec": 30}

_action_meta("network_policy_revert", catalog)
# → {"name": "network_policy_revert", "cost_min": 15, "blast_radius_services": 4}
# ↑ blast=4 → cực kỳ nguy hiểm, cần confidence rất cao
```

---

### Hàm 11: `_infer_params(action_name, query_vec, history_candidates) -> dict`

**Mục đích:** Tự động điền tham số cho action dựa trên context của incident.

```python
def _infer_params(action_name, query_vec, history_candidates) -> dict:
    top_svc = query_vec.get('top_error_trace_service')  # service bị lỗi nhiều nhất
    affected = query_vec.get('affected_services', [])

    if action_name == 'rollback_service':
        svc = top_svc or affected[0]
        return {'service': svc, 'target_version': 'previous'}

    elif action_name == 'increase_pool_size':
        svc = top_svc or affected[0]
        return {'service': svc, 'from_value': 50, 'to_value': 100}

    elif action_name == 'restart_pod':
        svc = top_svc or affected[0]
        return {'service': svc, 'pod_selector': 'app=' + svc}

    elif action_name == 'page_oncall':
        return {'team': 'platform-team'}
```

**Ví dụ E01:**
```python
query_vec = {
    "top_error_trace_service": "payment-svc",  # ← destination của lỗi
    "affected_services": ["checkout-svc", "payment-svc"]
}

_infer_params("increase_pool_size", query_vec, ...)
# → {"service": "payment-svc", "from_value": 50, "to_value": 100}
# ↑ Tăng pool của payment-svc từ 50 lên 100 connections

_infer_params("restart_pod", query_vec, ...)
# → {"service": "payment-svc", "pod_selector": "app=payment-svc"}
```

---

### Hàm 12: `select_action(retrieval_result, actions_catalog, query_vec) -> dict`

**Mục đích:** Hàm chính của Layer 3 — chọn action cuối cùng.

#### Bước 1: OOD check

```python
if is_ood:
    return _build_decision(action='page_oncall', confidence=0.0,
                           reason='OOD: max_similarity=0.214 < threshold=0.22')
```

#### Bước 2: Tính EV cho từng candidate

**Công thức EV:**
```
ev_p      = 0.6 × vote_frac + 0.4 × p_success
EV(a)     = ev_p × 1.0 - (1 - ev_p) × blast_penalty - 0.10 × cost_penalty
blast_penalty = blast_radius_services / 4.0
cost_penalty  = cost_min / 15.0
```

**Ví dụ tính EV cho E04 (lock_contention trên payments-db):**

```python
candidates = [
    {"action": "restart_pod",    "vote_frac": 0.685, "p_success": 1.0},
    {"action": "rollback_service","vote_frac": 0.176, "p_success": 0.333},
    {"action": "increase_pool_size","vote_frac":0.131,"p_success": 0.333},
]

# restart_pod: blast=1, cost=2
ev_p = 0.6 × 0.685 + 0.4 × 1.0 = 0.411 + 0.400 = 0.811
blast_penalty = 1/4 = 0.25
cost_penalty  = 2/15 = 0.133
EV = 0.811 - (1-0.811)×0.25 - 0.1×0.133
   = 0.811 - 0.047 - 0.013 = 0.751  ← WINNER

# rollback_service: blast=1, cost=10
ev_p = 0.6 × 0.176 + 0.4 × 0.333 = 0.106 + 0.133 = 0.239
EV = 0.239 - (0.761)×0.25 - 0.1×0.667
   = 0.239 - 0.190 - 0.067 = -0.018  ← ÂM! Không chọn

# increase_pool_size: blast=1, cost=1
ev_p = 0.6 × 0.131 + 0.4 × 0.333 = 0.079 + 0.133 = 0.212
EV = 0.212 - (0.788)×0.25 - 0.1×0.067
   = 0.212 - 0.197 - 0.007 = 0.008  ← Gần 0, thua restart_pod
```

**Tại sao rollback_service có EV âm?**

Vì `p_success` chỉ 0.333 (chỉ 1/3 lần dùng thành công) + cost=10 (đắt) → penalty quá lớn so với benefit.

#### Bước 3: Blast-radius gate

```python
blast_gate_triggered = (
    best['blast'] >= BLAST_GATE       # blast_radius >= 3
    and best['confidence'] < CONFIDENCE_GATE  # confidence < 0.50
)
min_confidence_gate = best['confidence'] < MIN_CONFIDENCE  # confidence < 0.20

if blast_gate_triggered or min_confidence_gate:
    return page_oncall  # escalate
```

**Ví dụ:**

```
Giả sử action tốt nhất là network_policy_revert (blast=4):
  confidence = 0.3 (< 0.5)
  blast = 4 (>= 3)
  → blast_gate_triggered = True → page_oncall

Ngược lại, restart_pod (blast=1):
  confidence = 0.814 (> 0.5)
  blast = 1 (< 3)
  → blast_gate_triggered = False → auto-act ✓
```

#### Bước 4: Tính confidence

```python
# Trong vòng lặp EV:
confidence = (0.6 * vote_frac + 0.4 * p_suc) * min(1.0, max_sim / 0.40)
#                                                    ↑
#                            Scale down nếu similarity yếu
```

**Ví dụ:**
```
E04: max_sim = 0.608 > 0.40
  confidence = 0.811 × min(1.0, 0.608/0.40) = 0.811 × 1.0 = 0.814

E03: max_sim = 0.242
  confidence = ev_p × min(1.0, 0.242/0.40) = ev_p × 0.605
  → confidence bị scale down vì similarity thấp
```

---

### Hàm 13: `_build_decision(...) -> dict`

**Mục đích:** Đóng gói tất cả thông tin thành 1 dict chuẩn để ghi vào `audit.jsonl`.

```python
def _build_decision(action, params, confidence, reason, ev, retrieval, query_vec, ev_table=None):
    return {
        'incident_id':     query_vec.get('incident_id', ''),
        'selected_action': action,
        'params':          params,
        'confidence':      round(confidence, 3),
        'consensus_score': retrieval.get('max_similarity', 0.0),
        'top_3_neighbors': retrieval.get('top_3_neighbors', []),
        'blast_radius_check': {
            'blast_gate':      BLAST_GATE,       # 3
            'confidence_gate': CONFIDENCE_GATE,  # 0.50
            'min_confidence':  MIN_CONFIDENCE,   # 0.20
        },
        'evidence': {
            'reason':           reason,
            'ev_table':         ev_table or [],
            'is_ood':           retrieval.get('is_ood', False),
            'max_similarity':   retrieval.get('max_similarity', 0.0),
            'affected_services': query_vec.get('affected_services', []),
            'top_error_edge':    query_vec.get('top_error_edge'),
            'candidates_raw':    retrieval.get('candidates', []),
        }
    }
```

---

## engine.py — Entry point

### Hàm 14: `decide(incident_path, history_path, actions_path) -> dict`

**Mục đích:** Orchestrate 3 layers — đọc file, gọi từng layer theo thứ tự.

```python
def decide(incident_path, history_path, actions_path) -> dict:
    incident = json.loads(incident_path.read_text())
    history  = json.loads(history_path.read_text())
    catalog  = yaml.safe_load(actions_path.read_text())

    vec      = extract_features(incident)    # Layer 1
    retrieval = retrieve_and_vote(vec, history)  # Layer 2
    decision  = select_action(retrieval, catalog, vec)  # Layer 3

    return decision
```

**Luồng data flow qua 3 layers:**

```
eval/E01.json
    │
    │ {"incident_id": "E01", "logs": [...], "traces": [...]}
    ▼
extract_features()
    │
    │ {"log_template_set": {...}, "affected_services": [...], ...}
    ▼
retrieve_and_vote()
    │
    │ {"candidates": [...], "top_3_neighbors": [...], "is_ood": False}
    ▼
select_action()
    │
    │ {"selected_action": "increase_pool_size", "confidence": 0.671, ...}
    ▼
audit.jsonl  (ghi thêm 1 dòng)
```

### Hàm 15: `main() -> int`

**Mục đích:** CLI interface, parse arguments, gọi `decide()`, ghi kết quả.

```python
def main():
    # Parse: python engine.py decide --incident eval/E01.json
    args = p.parse_args()

    if args.cmd == 'decide':
        out = decide(Path(args.incident), Path(args.history), Path(args.actions))
        print(json.dumps(out, indent=2))  # in ra terminal

        with open('audit.jsonl', 'a') as f:  # 'a' = append, không overwrite
            f.write(json.dumps(out) + '\n')

        return 0
```

---

## optional-helpers.py — Helpers phụ

### Hàm 16: `parse_history_action(s: str) -> dict`

**Mục đích:** Parse chuỗi action từ history format `"action:param1:param2"` thành dict.

```python
def parse_history_action(s: str) -> dict:
    parts = s.split(":")
    return {"name": parts[0], "params": parts[1:]}
```

**Ví dụ:**

```python
parse_history_action("rollback_service:payment-svc:v3.1")
# → {"name": "rollback_service", "params": ["payment-svc", "v3.1"]}

parse_history_action("increase_pool_size:payment-svc:50:100")
# → {"name": "increase_pool_size", "params": ["payment-svc", "50", "100"]}

parse_history_action("restart_pod:payments-db:default")
# → {"name": "restart_pod", "params": ["payments-db", "default"]}

parse_history_action("page_oncall:platform-team")
# → {"name": "page_oncall", "params": ["platform-team"]}
```

**Map params sang named params** (theo `actions.yaml`):

| Action | Params positional | Named params |
|--------|------------------|--------------|
| `rollback_service` | `[service, target_version]` | `{"service": "payment-svc", "target_version": "v3.1"}` |
| `increase_pool_size` | `[service, from_value, to_value]` | `{"service": "payment-svc", "from_value": 50, "to_value": 100}` |
| `restart_pod` | `[service, pod_selector]` | `{"service": "payments-db", "pod_selector": "default"}` |

---

### Hàm 17: `parse_metric_delta(s: str) -> tuple[float, float]`

**Mục đích:** Parse chuỗi delta `"30 -> 99"` thành tuple `(before, after)`.

```python
def parse_metric_delta(s: str) -> tuple[float, float]:
    parts = s.replace("->", "|").split("|")
    return float(parts[0].strip()), float(parts[1].strip())
```

**Ví dụ:**

```python
parse_metric_delta("30 -> 99")       # → (30.0, 99.0)
parse_metric_delta("0.001 -> 1.0")   # → (0.001, 1.0)
parse_metric_delta("50->100")        # → (50.0, 100.0)
parse_metric_delta("200 -> 5400")    # → (200.0, 5400.0)
```

**Dữ liệu thực trong incidents_history.json:**

```json
"metric_signatures": [
  {"service": "payment-svc", "metric": "conn_pool_used", "delta": "30 -> 95"},
  {"service": "payments-db", "metric": "lock_wait_ms",   "delta": "10 -> 800"}
]
```

```python
parse_metric_delta("30 -> 95")   # → (30.0, 95.0)  ← pool dùng từ 30 lên 95%
parse_metric_delta("10 -> 800")  # → (10.0, 800.0) ← lock wait tăng 80×!
```

---

## grade.py — Chấm điểm tự động

### Hàm 18: `action_matches(recommended: dict, accepted: dict) -> bool`

**Mục đích:** Kiểm tra action engine chọn có khớp với đáp án không.

```python
def action_matches(recommended: dict, accepted: dict) -> bool:
    # Bước 1: action name phải giống
    if recommended.get("selected_action") != accepted.get("name"):
        return False
    # Bước 2: params đáp án phải là subset của params engine chọn
    accepted_params = accepted.get("params", {}) or {}
    rec_params = recommended.get("params", {}) or {}
    for k, v in accepted_params.items():
        if rec_params.get(k) != v:
            return False
    return True
```

**Ví dụ:**

```python
recommended = {"selected_action": "increase_pool_size",
               "params": {"service": "payment-svc", "from_value": 50, "to_value": 100}}

# Test 1: Đúng action, đúng params
accepted = {"name": "increase_pool_size", "params": {"service": "payment-svc"}}
action_matches(recommended, accepted)  # → True

# Test 2: Đúng action, sai service
accepted = {"name": "increase_pool_size", "params": {"service": "checkout-svc"}}
action_matches(recommended, accepted)  # → False

# Test 3: Sai action
accepted = {"name": "rollback_service", "params": {}}
action_matches(recommended, accepted)  # → False
```

---

### Hàm 19: `main()` trong grade.py

**Mục đích:** Đọc audit.jsonl và expected.json, chấm điểm từng incident.

**Cấu trúc expected.json (giả định):**
```json
{
  "E01": {
    "accepted_actions": [
      {"name": "increase_pool_size", "params": {"service": "payment-svc"}},
      {"name": "rollback_service", "params": {}}
    ],
    "must_not_action": "page_oncall"   ← KHÔNG ĐƯỢC chọn cái này
  },
  "E07": {
    "accepted_actions": [
      {"name": "page_oncall", "params": {}}
    ]
  }
}
```

**Logic chấm điểm:**
```python
for eid, expected_entry in expected.items():
    rec = by_id.get(eid)  # lấy từ audit.jsonl

    # Trường hợp 1: vi phạm must_not_action
    if must_not and rec.get("selected_action") == must_not:
        forbidden += 1
        # → E01 chọn page_oncall → VIOLATION!

    # Trường hợp 2: action đúng
    elif any(action_matches(rec, a) for a in accepted):
        correct += 1
        # → E01 chọn increase_pool_size → OK

    # Trường hợp 3: sai
    else:
        detail.append((eid, f"WRONG -> {rec['selected_action']}"))
```

**Output chạy thực tế:**
```
Correct: 8/8
Forbidden (chose must_not_action): 0/8
Missing from audit: 0/8

Per-incident detail:
  E01: OK -> increase_pool_size
  E02: OK -> rollback_service
  E03: OK -> rollback_service
  E04: OK -> restart_pod
  E05: OK -> increase_pool_size
  E06: OK -> increase_pool_size
  E07: OK -> page_oncall
  E08: OK -> page_oncall

Auto-rubric estimate: 85/85
```

---

## 📊 Tóm tắt toàn bộ hàm

| # | Hàm | File | Tác dụng |
|---|-----|------|----------|
| 1 | `_drain_template()` | features.py | Chuẩn hoá 1 dòng log thành template |
| 2 | `extract_log_templates()` | features.py | 500 logs → dict {template: count} |
| 3 | `extract_trace_features()` | features.py | Tính error_rate, p99_deviation cho mỗi trace edge |
| 4 | `extract_affected_services()` | features.py | Xác định service nào đang bị ảnh hưởng |
| 5 | `extract_features()` | features.py | **Hàm tổng hợp Layer 1** → incident_vector |
| 6 | `_drain_tokens()` | retrieval.py | Tokenise string → set để tính Jaccard |
| 7 | `_jaccard()` | retrieval.py | Tính Jaccard similarity giữa 2 tập hợp |
| 8 | `similarity()` | retrieval.py | Tính similarity tổng hợp 3 thành phần |
| 9 | `retrieve_and_vote()` | retrieval.py | **Hàm tổng hợp Layer 2** → candidates + OOD |
| 10 | `_action_meta()` | decision.py | Tra cứu metadata action từ catalog |
| 11 | `_infer_params()` | decision.py | Tự điền params cho action |
| 12 | `select_action()` | decision.py | **Hàm tổng hợp Layer 3** → quyết định cuối |
| 13 | `_build_decision()` | decision.py | Đóng gói kết quả thành audit dict |
| 14 | `decide()` | engine.py | Orchestrate 3 layers |
| 15 | `main()` engine | engine.py | CLI entry point |
| 16 | `parse_history_action()` | optional-helpers.py | Parse "action:param1:param2" → dict |
| 17 | `parse_metric_delta()` | optional-helpers.py | Parse "30 -> 99" → (30.0, 99.0) |
| 18 | `action_matches()` | grade.py | Kiểm tra action có đúng đáp án không |
| 19 | `main()` grade | grade.py | Chấm điểm audit.jsonl |

---

## 🔑 Các con số quan trọng cần nhớ

| Hằng số | Giá trị | Ý nghĩa |
|---------|---------|---------|
| `OOD_THRESHOLD` | 0.22 | Dưới ngưỡng này → incident lạ → escalate |
| `TOP_K` | 5 | Lấy 5 neighbor gần nhất |
| `BLAST_GATE` | 3 | blast_radius ≥ 3 cần kiểm tra thêm |
| `CONFIDENCE_GATE` | 0.50 | Cần ≥ 50% confidence để act khi blast cao |
| `MIN_CONFIDENCE` | 0.20 | Dưới 20% → luôn escalate |
| `outcome_weight.success` | 1.0 | Vote đầy đủ nếu outcome = success |
| `outcome_weight.partial` | 0.5 | Vote nửa nếu outcome = partial |
| `outcome_weight.failed` | 0.1 | Vote rất ít nếu outcome = failed |
| Trọng số log | 0.45 | Log similarity chiếm 45% |
| Trọng số trace | 0.35 | Trace similarity chiếm 35% |
| Trọng số service | 0.20 | Service overlap chiếm 20% |

---

*Notebook này được tạo tự động từ codebase của Evidence-Driven Remediation Engine.*
