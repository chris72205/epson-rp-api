import os
from dataclasses import dataclass


def _truthy(value):
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    usb_vendor: int = 0x04B8
    usb_product: int = 0x0202
    # escpos' capabilities DB has no TM-T88VI entry; the TM-T88V profile is
    # command-compatible (the VI added interfaces, not new print commands).
    profile: str = "TM-T88V"
    paper_width_dots: int = 512
    usb_timeout_ms: int = 5000
    host: str = "0.0.0.0"
    port: int = 8080
    queue_max: int = 50
    history_max: int = 100
    log_level: str = "INFO"
    fake: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        return cls(
            usb_vendor=int(env.get("PRINTER_USB_VENDOR", "0x04b8"), 16),
            usb_product=int(env.get("PRINTER_USB_PRODUCT", "0x0202"), 16),
            profile=env.get("PRINTER_PROFILE", "TM-T88V"),
            paper_width_dots=int(env.get("PAPER_WIDTH_DOTS", "512")),
            usb_timeout_ms=int(env.get("USB_TIMEOUT_MS", "5000")),
            host=env.get("HOST", "0.0.0.0"),
            port=int(env.get("PORT", "8080")),
            queue_max=int(env.get("QUEUE_MAX", "50")),
            history_max=int(env.get("HISTORY_MAX", "100")),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
            fake=_truthy(env.get("PRINTER_FAKE", "")),
        )
