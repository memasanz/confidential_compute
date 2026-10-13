"""No proposed lifetime or data-limit values are production defaults."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    idle: int
    absolute: int
    queue_wait: int
    queue_size: int
    operation: int
    claim: int
    upload_bytes: int
    result_bytes: int
    provisioning_age: int = 900
    terminal_retention: int = 86400
    max_total: int = 20
    desired_ready: int = 10

    def __post_init__(self):
        if any(type(v) is not int or v < 0 or (v == 0 and name != "desired_ready")
               for name, v in vars(self).items()):
            raise ValueError("All limits must be explicitly positive integers (desired_ready may be 0)")
        if not 1 <= self.max_total <= 20 or self.desired_ready > min(10, self.max_total):
            raise ValueError("Maximum total must be 1..20 and desired ready at most min(10, maximum)")
        if self.queue_size > 50:
            raise ValueError("Queue size must be <=50 for bounded same-partition transactions")
        if self.operation >= self.claim:
            raise ValueError("Claim lifetime must exceed the bounded external operation")

    @classmethod
    def environment(cls):
        values = {name: int(os.environ["POOL_" + name.upper()])
                  for name in ("idle", "absolute", "queue_wait", "queue_size",
                               "operation", "claim", "upload_bytes", "result_bytes",
                               "provisioning_age", "terminal_retention")}
        # Optional pilot overrides; absent means the approved 20 maximum / 10 ready.
        for name in ("max_total", "desired_ready"):
            if "POOL_" + name.upper() in os.environ:
                values[name] = int(os.environ["POOL_" + name.upper()])
        return cls(**values)
