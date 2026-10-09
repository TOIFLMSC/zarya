import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    data_dir: Path
    web_dir: Path
    host: str = "127.0.0.1"
    port: int = 8787
    session_seconds: int = 8 * 60 * 60
    dev_ui: bool = False
    telegram_token: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    prompts_file: Path | None = None

    @property
    def origins(self) -> set[str]:
        return {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}

    @classmethod
    def from_env(cls) -> "Config":
        root = Path(__file__).resolve().parents[2]
        data_dir = Path(os.environ.get("ZARYA_DATA_DIR", root / "data")).resolve()
        token = os.environ.get("ZARYA_TELEGRAM_TOKEN", "").strip()
        token_file = data_dir / "telegram-token.txt"
        if not token and token_file.is_file():
            token = token_file.read_text(encoding="utf-8-sig").strip()
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        key_file = data_dir / "openai-api-key.txt"
        if not api_key and key_file.is_file():
            api_key = key_file.read_text(encoding="utf-8-sig").strip()
        return cls(
            data_dir=data_dir,
            web_dir=Path(os.environ.get("ZARYA_WEB_DIR", root / "web" / "dist")).resolve(),
            port=int(os.environ.get("ZARYA_PORT", "8787")),
            dev_ui=os.environ.get("ZARYA_DEV_UI") == "1",
            telegram_token=token,
            openai_api_key=api_key,
            ffmpeg_path=os.environ.get("ZARYA_FFMPEG", ""),
            ffprobe_path=os.environ.get("ZARYA_FFPROBE", ""),
            prompts_file=Path(
                os.environ.get("ZARYA_PROMPTS_FILE", data_dir / "prompts.json")
            ).resolve(),
        )
