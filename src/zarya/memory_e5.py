"""Local E5 index. SQLite authorizes every candidate before it enters a reply."""

import argparse
import asyncio
import hashlib
import logging
import tempfile
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from time import time
from typing import Any
from urllib.request import urlopen

from zarya.database import Database
from zarya.memory_logic import RETENTION

logger = logging.getLogger(__name__)
ASSETS = Path(__file__).parent / "e5_assets"
REVISION = "761b726dd34fb83930e26aab4e9ac3899aa1fa78"
HASHES = {
    "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    "model_quantized.onnx": "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
}
Ref = tuple[str, int, int, str]
Ranking = tuple[Ref, ...]


def query_text(message: dict[str, Any]) -> str:
    return str(message.get("text") or message.get("caption") or "")[:4000]


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def fetch_assets(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/Xenova/multilingual-e5-small/resolve/{REVISION}"
    for name, expected in HASHES.items():
        target = folder / name
        if target.is_file() and digest(target) == expected:
            continue
        prefix = "onnx/" if name.endswith(".onnx") else ""
        with tempfile.TemporaryDirectory(dir=folder) as temp:
            candidate = Path(temp) / name
            with urlopen(f"{base}/{prefix}{name}", timeout=45) as src, candidate.open("wb") as dst:
                total = 0
                while block := src.read(1024 * 1024):
                    total += len(block)
                    if total > 150 * 1024 * 1024:
                        raise ValueError("E5 asset too large")
                    dst.write(block)
            if digest(candidate) != expected:
                raise ValueError(f"Unexpected E5 asset: {name}")
            candidate.replace(target)


class Encoder:
    def __init__(self, assets: Path):
        for name, expected in HASHES.items():
            if digest(assets / name) != expected:
                raise ValueError(f"Unexpected E5 asset: {name}")
        self.np: Any = import_module("numpy")
        ort: Any = import_module("onnxruntime")
        tokenizers: Any = import_module("tokenizers")
        self.tokenizer = tokenizers.Tokenizer.from_file(str(assets / "tokenizer.json"))
        self.tokenizer.enable_padding(pad_id=1, pad_token="<pad>")
        self.tokenizer.enable_truncation(max_length=512)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(assets / "model_quantized.onnx"), opts, providers=["CPUExecutionProvider"]
        )

    def encode(self, lines: list[str]) -> Any:
        np = self.np
        batch = self.tokenizer.encode_batch(lines)
        fields = {
            "input_ids": [t.ids for t in batch],
            "attention_mask": [t.attention_mask for t in batch],
            "token_type_ids": [t.type_ids for t in batch],
        }
        inputs = {
            i.name: np.asarray(fields[i.name], dtype=np.int64) for i in self.session.get_inputs()
        }
        (hidden,) = self.session.run(None, inputs)
        mask = inputs["attention_mask"][:, :, None].astype(np.float32)
        means = (hidden * mask).sum(axis=1) / mask.sum(axis=1)
        return means / np.linalg.norm(means, axis=1, keepdims=True)


class LocalIndex:
    def __init__(self, db: Database, assets: Path = ASSETS):
        self.db = db
        self.assets = assets
        self.encoder: Encoder | None = None
        self.index: tuple[str, Ranking, Any] | None = None
        self.worker: asyncio.Task[None] | None = None
        self.next_refresh = 0.0
        self.active_bot: str | None = None

    async def close(self) -> None:
        if self.worker is not None:
            await self.worker
        self.index = None

    def deactivate(self) -> None:
        self.active_bot = None
        self.index = None

    def schedule(self, bot: str) -> None:
        if self.active_bot != bot:
            self.active_bot = bot
            self.next_refresh = 0.0
        if time() >= self.next_refresh and (self.worker is None or self.worker.done()):
            self.next_refresh = time() + 5
            self.worker = asyncio.create_task(self.refresh(bot), name="memory-e5-index")

    async def rank(self, bot: str, query: str) -> Ranking | None:
        if not query.strip():
            return None
        current, encoder = self.index, self.encoder
        if current is None or current[0] != bot or encoder is None:
            return None
        _, refs, matrix = current
        if not refs:
            return ()
        try:
            return await asyncio.to_thread(self.order, encoder, refs, matrix, query[:4000])
        except Exception:
            logger.warning("Local E5 query failed.")
            return None

    @staticmethod
    def order(encoder: Encoder, refs: Ranking, matrix: Any, text: str) -> Ranking:
        scores = matrix @ encoder.encode(["query: " + text])[0]
        return tuple(refs[i] for i in encoder.np.argsort(-scores, kind="stable"))

    async def refresh(self, bot: str) -> None:
        try:
            if self.encoder is None:
                self.encoder = await asyncio.to_thread(Encoder, self.assets)
            sql = """
                SELECT 'f', f.id, f.version, f.text FROM memory_facts f
                JOIN telegram_access a ON a.bot_id=f.bot_id AND a.subject_id=f.chat_id
                    AND a.scope=f.scope AND a.version=f.access_version
                WHERE f.bot_id=? AND f.updated_at>? AND f.state IN ('active','proposed')
                    AND a.state='approved' AND a.member_state NOT IN ('left','kicked','migrated')
                UNION ALL
                SELECT 'e', b.id, 1, b.summary FROM memory_batches b
                JOIN telegram_access a ON a.bot_id=b.bot_id AND a.subject_id=b.chat_id
                    AND a.scope=b.scope AND a.version=b.access_version
                WHERE b.bot_id=? AND b.created_at>? AND b.state='completed'
                    AND b.summary IS NOT NULL
                    AND a.state='approved' AND a.member_state NOT IN ('left','kicked','migrated')
                ORDER BY 1,2
            """
            cutoff = datetime.fromtimestamp(time() - RETENTION, UTC).isoformat()
            rows = await self.db.all(sql, (bot, cutoff, bot, cutoff))
            refs, matrix = await asyncio.to_thread(self.build, self.encoder, bot, rows, self.index)
            if self.active_bot == bot:
                self.index = (bot, refs, matrix)
        except Exception:
            self.next_refresh = time() + 30
            logger.warning("Local E5 index unavailable. Check model assets and runtime.")

    @staticmethod
    def build(
        encoder: Encoder, bot: str, rows: list[Any], old: tuple[str, Ranking, Any] | None
    ) -> tuple[Ranking, Any]:
        refs: Ranking = tuple(
            (kind, ident, version, text_hash(text)) for kind, ident, version, text in rows
        )
        if old is not None and old[0] == bot and refs == old[1]:
            return refs, old[2]
        vectors = (
            dict(zip(old[1], old[2], strict=True)) if old is not None and old[0] == bot else {}
        )
        missing = [
            (ref, "passage: " + row[3])
            for ref, row in zip(refs, rows, strict=True)
            if ref not in vectors
        ]
        for start in range(0, len(missing), 16):
            batch = missing[start : start + 16]
            vectors.update(
                zip((r for r, _ in batch), encoder.encode([t for _, t in batch]), strict=True)
            )
        matrix = (
            encoder.np.stack([vectors[ref] for ref in refs]) if refs else encoder.np.empty((0, 384))
        )
        return refs, matrix


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ASSETS)
    fetch_assets(parser.parse_args().output)
