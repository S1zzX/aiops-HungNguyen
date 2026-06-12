import concurrent.futures, requests, time, json

body = json.load(open("body.json", encoding="utf-8-sig"))

def req():
    t0 = time.time()
    r = requests.post("http://localhost:8000/incident", json=body)
    return r.status_code, time.time() - t0

with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    t0 = time.time()
    results = list(ex.map(lambda _: req(), range(4)))
    total = time.time() - t0

for status, dur in results:
    print(status, round(dur, 2))
print("total wall time:", round(total, 2))