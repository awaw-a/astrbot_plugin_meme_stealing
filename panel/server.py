from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from threading import Thread
from typing import Any

try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
    from pydantic import BaseModel
except Exception:  # noqa: BLE001 - 允许 AstrBot 未安装依赖时禁用面板。
    FastAPI = None  # type: ignore[assignment]
    BaseModel = object  # type: ignore[assignment,misc]

try:
    from ..database import MemeDatabase
except ImportError:
    from database import MemeDatabase


class MemeUpdate(BaseModel):  # type: ignore[misc]
    description: str | None = None
    tags: list[str] | None = None
    emotion: list[str] | None = None
    enabled: bool | None = None
    pending_review: bool | None = None


class MemeBatchDelete(BaseModel):  # type: ignore[misc]
    ids: list[int]


class PanelServer:
    def __init__(self, db: MemeDatabase, host: str, port: int, token: str):
        self.db = db
        self.host = host
        self.port = port
        self.token = token
        self._server: Any = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def start(self) -> None:
        if FastAPI is None:
            raise RuntimeError("缺少 fastapi/uvicorn 依赖，请先安装 requirements.txt")
        if self._thread and self._thread.is_alive():
            return

        import uvicorn

        app = create_app(self.db, self.token)
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._thread = Thread(target=self._server.run, name="meme-panel", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            await asyncio.to_thread(self._thread.join, 3)


def create_app(db: MemeDatabase, token: str):
    if FastAPI is None:
        raise RuntimeError("缺少 fastapi 依赖")

    app = FastAPI(title="AstrBot Meme Stealing Panel")
    root = Path(__file__).resolve().parent
    template_path = root / "templates" / "index.html"
    style_path = root / "static" / "style.css"

    async def require_token(
        request: Request,
        token_query: str = Query(default="", alias="token"),
        x_admin_token: str = Header(default=""),
        authorization: str = Header(default=""),
    ) -> None:
        supplied = token_query or x_admin_token
        if authorization.lower().startswith("bearer "):
            supplied = authorization.split(" ", 1)[1].strip()
        if not token or supplied != token:
            raise HTTPException(status_code=401, detail="invalid token")
        request.state.token = supplied

    @app.get("/", response_class=HTMLResponse)
    async def index():
        html = template_path.read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/static/style.css")
    async def css():
        return FileResponse(style_path, media_type="text/css")

    @app.get("/api/memes")
    async def list_memes(
        _: None = Depends(require_token),
        q: str = "",
        status: str = "",
        limit: int = 60,
        offset: int = 0,
    ):
        pending: bool | None = None
        enabled: bool | None = None
        if status == "pending":
            pending = True
        elif status == "disabled":
            enabled = False
        elif status == "enabled":
            enabled = True
            pending = False
        records = db.list_memes(
            query=q,
            pending=pending,
            enabled=enabled,
            limit=min(max(limit, 1), 200),
            offset=max(offset, 0),
        )
        return {
            "items": [record.to_dict() for record in records],
            "stats": db.stats(),
            "has_more": len(records) >= min(max(limit, 1), 200),
        }

    @app.get("/api/stats")
    async def stats(_: None = Depends(require_token)):
        return db.stats()

    @app.patch("/api/memes/{meme_id}")
    async def update_meme(
        meme_id: int, payload: MemeUpdate, _: None = Depends(require_token)
    ):
        record = db.update_meme(
            meme_id,
            description=payload.description,
            tags=payload.tags,
            emotion=payload.emotion,
            enabled=payload.enabled,
            pending_review=payload.pending_review,
        )
        if not record:
            raise HTTPException(status_code=404, detail="not found")
        return record.to_dict()

    @app.delete("/api/memes/{meme_id}")
    async def delete_meme(meme_id: int, _: None = Depends(require_token)):
        if not db.delete_meme(meme_id):
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse({"ok": True})

    @app.post("/api/memes/batch-delete")
    async def batch_delete_meme(
        payload: MemeBatchDelete, _: None = Depends(require_token)
    ):
        ids = sorted({int(item) for item in payload.ids if int(item) > 0})
        if not ids:
            raise HTTPException(status_code=400, detail="empty ids")
        deleted: list[int] = []
        missing: list[int] = []
        for meme_id in ids:
            if db.delete_meme(meme_id):
                deleted.append(meme_id)
            else:
                missing.append(meme_id)
        return {"ok": True, "deleted": deleted, "missing": missing}

    @app.get("/images/{meme_id}")
    async def image(meme_id: int, _: None = Depends(require_token)):
        record = db.get_meme(meme_id)
        if not record:
            raise HTTPException(status_code=404, detail="not found")
        path = Path(record.file_path)
        if not path.exists():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run meme stealing management panel.")
    parser.add_argument("--db", required=True, help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8756)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()

    if FastAPI is None:
        raise SystemExit(
            "缺少 fastapi/uvicorn 依赖，请先运行: pip install -r requirements.txt"
        )

    import uvicorn

    database = MemeDatabase(Path(args.db))
    uvicorn.run(create_app(database, args.token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
