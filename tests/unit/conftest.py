import json

import pytest
import sentry_sdk
from sentry_sdk.transport import Transport
from temporalio.testing import WorkflowEnvironment

from paidyet.config import ROOT


@pytest.fixture(scope="session")
async def env():
    cache = ROOT / ".pytest_cache" / "temporal"  # the test server binary stays inside the repo
    cache.mkdir(parents=True, exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(cache)) as env:
        yield env


class CaptureTransport(Transport):
    """Keeps everything Sentry would send, in memory."""

    def __init__(self, options=None):
        super().__init__(options)
        self.items: list[dict] = []

    def capture_envelope(self, envelope):
        for item in envelope.items:
            if item.payload.json is not None:
                self.items.append({"type": item.type, **item.payload.json})


class SentryCapture:
    def __init__(self, transport: CaptureTransport):
        self.transport = transport

    def flush(self) -> list[dict]:
        sentry_sdk.flush()
        return self.transport.items

    def transactions(self) -> list[dict]:
        return [i for i in self.flush() if i["type"] == "transaction"]

    def spans(self) -> list[dict]:
        """All spans as {op, name, data, trace_id}: classic transaction spans and streamed span-v2 items
        (sentry-sdk streams gen_ai spans separately, linked by trace and parent span IDs)."""
        out = []
        for item in self.flush():
            if item["type"] == "transaction":
                out += [{"op": s["op"], "name": s.get("description"), "data": s.get("data", {}),
                         "trace_id": s["trace_id"]} for s in item.get("spans", [])]  # fmt: skip
            elif item["type"] == "span":
                for s in item.get("items", []):
                    attrs = {k: v["value"] for k, v in s.get("attributes", {}).items()}
                    out.append({"op": attrs.get("sentry.op"), "name": s["name"], "data": attrs, "trace_id": s["trace_id"]})
        return out

    def everything(self) -> str:
        return json.dumps(self.flush(), ensure_ascii=False)


@pytest.fixture
def sentry():
    """Sentry configured exactly like main.py, but sending to memory."""
    from main import sentry_options

    transport = CaptureTransport()
    sentry_sdk.init(**sentry_options("https://public@sentry.example.invalid/1"), transport=transport)
    yield SentryCapture(transport)
    sentry_sdk.init()  # back to disabled for other tests
