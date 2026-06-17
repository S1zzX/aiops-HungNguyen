# W3-D3 Submission — HungNguyen

## Outage chosen
- ID: 3
- Name: Cloudflare WAF Regex (2019-07-02)
- Why this one: I wanted to work with a failure mode that is purely
  code/data-dependent rather than infrastructure-dependent — a regex pattern
  that looks completely safe on most inputs but is exponential on others felt
  like the kind of bug that's easy to miss in review and easy to reproduce
  cheaply (no multi-node network setup needed, unlike the GitHub split-brain
  case).
- Failure mode: catastrophic backtracking (regex), reinforced by a global
  atomic deploy with no canary buffer.

## 3 thứ tôi học từ outage này
1. Catastrophic backtracking is genuinely data-dependent, not just
   theoretical — I measured it directly on the pack's actual regex
   (`(?:(?:"|\d|.*)+(?:.*=.*))`): an input of 20 characters took 0.13s, but
   25 characters (just 5 more) took 4.17s, and 30 characters didn't finish
   inside a 25-second window. That's the exponential blowup made concrete,
   not just something I read about.
2. A pipeline's RCA can be "correct" by its own logic (topology-aware,
   confidence 0.7, traced `frontend` → `api-gateway` correctly) and still miss
   the actual root cause, because the topology graph only has service-level
   nodes — it has no concept of "a rule deployed inside this service's
   middleware." The RCA output is accurate at the abstraction level it
   operates on, but that abstraction level isn't fine-grained enough for this
   incident class.
3. Detection in my reproduction only happened because I, externally, wrote a
   prober that explicitly measured latency and called `/ingest`. The pipeline
   itself has zero ability to notice that a previously-fast endpoint became
   slow — it is purely reactive to whatever gets pushed to it (or whatever
   Prometheus happens to expose, which fails silently to an empty list on any
   connection issue). That's a much bigger gap than I expected going in.

## 1 thứ pipeline của tôi sẽ vẫn miss nếu outage này xảy ra real
- Pattern: any silent-CPU-pin failure on a route that nobody has registered a
  synthetic prober for.
- Why miss: the pipeline's only detection paths are (a) something explicitly
  calling `/ingest`, or (b) a Prometheus query that itself depends on metrics
  already being exported correctly. Neither path activates on its own just
  because a route got slow — there's no native "this used to take 50ms and now
  takes 4000ms" check inside the pipeline.
- Mitigation idea: ADR-001 — bring synthetic latency probing in-house as a
  first-class pipeline component with a baseline-comparison alert, instead of
  depending on an externally-maintained prober that may or may not exist for
  any given route.

## 1 quyết định trong ADR mà tôi không hoàn toàn chắc
I'm not fully sure about the baseline-comparison threshold design in ADR-001 —
I specified "latency exceeds its baseline by a defined multiple" but didn't
pin down what that multiple should be or how the baseline should be computed
(rolling average? p50? p99 from the last N minutes?). A multiple that's too
tight will false-positive on normal traffic variance; too loose and it won't
catch a real regression early enough. I think this needs to be tuned per
endpoint rather than set globally, but I didn't want to overclaim a number I
haven't actually validated against real traffic patterns.

## Cost model verdict cho stack của tôi
- ROI: 2.0
- Payback: 0.5 tháng
- Verdict: worth_it
