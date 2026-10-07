"""Demo flow from spec §3.3: an io fetch, a cpu analysis and an async publish.

python -m miniflow validate examples/pipeline.py
python -m miniflow graph examples/pipeline.py -o pipeline.dot
"""

import asyncio
import time

from miniflow import task


@task(kind="io", retries=3, args=("https://example.com/data",))
def fetch(url):
    time.sleep(0.2)  # stands in for a network call
    return list(range(200_000))


@task(kind="cpu", after=[fetch])
def analyse(data):
    return sum(x * x for x in data)


@task(kind="async", after=[analyse])
async def publish(report):
    await asyncio.sleep(0.1)
    return f"published {report}"
