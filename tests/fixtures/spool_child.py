import os
import sys
import time

mode, marker = sys.argv[1], sys.argv[2]
with open(marker, "w") as handle:
    handle.write("effect")
if mode == "file":
    sys.stdout.write("x" * (1024 * 1024))
elif mode == "fail":
    sys.stderr.write("real failure")
    sys.exit(3)
elif mode == "hang":
    sys.stdout.write("partial-start")
    sys.stdout.flush()
    time.sleep(60)
elif mode == "flood":
    chunk = b"y" * 65536
    while True:
        os.write(1, chunk)
