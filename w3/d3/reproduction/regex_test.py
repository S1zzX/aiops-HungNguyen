import re, time, sys

print(f"Python version: {sys.version}")
EVIL = re.compile(r'(?:(?:"|\d|.*)+(?:.*=.*))')

for n in [20, 24, 26, 28, 32, 40]:
    inp = "x" * n
    t0 = time.time()
    EVIL.match(inp)
    dt = time.time() - t0
    print(f"n={n:3d}: {dt*1000:8.1f}ms")
