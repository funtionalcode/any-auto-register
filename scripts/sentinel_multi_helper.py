#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from platforms.chatgpt.sentinel_batch import (  # noqa: E402
    ConfigResolver,
    SentinelBatchService,
    write_batch_result,
)


def main() -> int:
    resolver = ConfigResolver()
    config = resolver.resolve()
    service = SentinelBatchService()
    result = service.generate(config)
    write_batch_result(result, config.output_path)
    print(result.to_json())
    print(str(config.output_path))
    return 1 if result.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
