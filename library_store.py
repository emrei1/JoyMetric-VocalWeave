from __future__ import annotations
import json, os, shutil, sqlite3, threading, time, uuid
from pathlib import Path
from typing import Any

COLLECTIONS = {"history", "projects", "audio", "favorites", "references"}

class LibraryStore:
    def __init__(self, app_root: Path):
        local = os.environ.get("LOCALAPPDATA")
        self.root = (Path(local) / "JoyMetric" / "userdata") if local else (Path.home() / ".joymetric" / "userdata")
        self.media_root = self.root / "library"
        self.db_path = self.root / "library.sqlite3"
        self.root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _init_db(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS library_items (
              id TEXT PRIMARY KEY, collection TEXT NOT NULL, title TEXT NOT NULL,
              created_at REAL NOT NULL, source_job_id TEXT, asset_kind TEXT,
              prompt TEXT, original_name TEXT, backend TEXT, duration REAL,
              assets_json TEXT NOT NULL, params_json TEXT NOT NULL, extra_json TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_collection_created ON library_items(collection, created_at DESC)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_source_job ON library_items(source_job_id)")

    @staticmethod
    def _link_or_copy(src: Path, dst: Path):
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists(): dst.unlink()
        try: os.link(src, dst)
        except OSError: shutil.copy2(src, dst)

    def _item_dir(self, collection, item_id):
        return self.media_root / collection / item_id

    def _copy_assets(self, collection, item_id, job_dir, outputs, keys):
        d = self._item_dir(collection, item_id); d.mkdir(parents=True, exist_ok=True)
        saved, used = {}, set()
        for key in keys:
            rel = outputs.get(key)
            if not rel: continue
            src = job_dir / rel
            if not src.exists() or not src.is_file(): continue
            name = src.name if src.name.lower() not in used else f"{key}_{src.name}"
            used.add(name.lower())
            self._link_or_copy(src, d / name)
            saved[key] = name
        return saved

    def _insert(self, collection, item_id, title, job_id, asset_kind, prompt, original_name, backend, duration, assets, params, extra):
        with self._connect() as db:
            db.execute("""INSERT INTO library_items
              (id,collection,title,created_at,source_job_id,asset_kind,prompt,original_name,backend,duration,assets_json,params_json,extra_json)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (item_id,collection,(title or "Untitled Track")[:160],time.time(),job_id,asset_kind,prompt or "",original_name or "",backend or "",float(duration or 0),json.dumps(assets),json.dumps(params or {}),json.dumps(extra or {})))

    def record_history(self, job_id, job_dir, state):
        outputs, params = state.get("outputs") or {}, state.get("params") or {}
        if not outputs: return None
        with self._lock:
            with self._connect() as db:
                row = db.execute("SELECT id FROM library_items WHERE collection='history' AND source_job_id=? LIMIT 1", (job_id,)).fetchone()
                if row: return row["id"]
            item_id = uuid.uuid4().hex[:16]
            assets = self._copy_assets("history", item_id, job_dir, outputs, ["modified_full"])
            if not assets: return None
            original_name = params.get("original_name") or "Untitled Track"
            title = (params.get("project_title") or Path(original_name).stem or original_name or "Untitled Track")[:160]
            self._insert("history", item_id, title, job_id, "modified", params.get("prompt",""), original_name, outputs.get("backend",""), outputs.get("duration_seconds",0), assets, params, {"steps":outputs.get("steps"),"processing_seconds":outputs.get("processing_seconds")})
            return item_id

    def save_from_job(self, collection, job_id, job_dir, state, asset_kind="modified", title_override=None):
        if collection not in COLLECTIONS - {"history"}: raise ValueError("Unsupported library collection")
        outputs, params = state.get("outputs") or {}, state.get("params") or {}
        if not outputs: raise ValueError("This render is not complete yet")
        if asset_kind not in {"modified","original"}: asset_kind="modified"
        item_id = uuid.uuid4().hex[:16]
        if collection == "projects":
            keys=["original_full","modified_full","modified_full_wav","protected_vocals","original_instrumental","edited_instrumental","edited_vocals"]; saved_kind="project"
        elif asset_kind == "original": keys=["original_full"]; saved_kind="original"
        else: keys=["modified_full"]; saved_kind="modified"
        with self._lock:
            assets = self._copy_assets(collection,item_id,job_dir,outputs,keys)
            if not assets:
                shutil.rmtree(self._item_dir(collection,item_id), ignore_errors=True)
                raise ValueError("No render file was available to save")
            original_name=params.get("original_name") or "Untitled Track"
            title = (title_override or params.get("project_title") or Path(original_name).stem or original_name or "Untitled Track")[:160]
            self._insert(collection,item_id,title,job_id,saved_kind,params.get("prompt",""),original_name,outputs.get("backend",""),outputs.get("duration_seconds",0),assets,params,{"steps":outputs.get("steps"),"processing_seconds":outputs.get("processing_seconds")})
        return self.get(collection,item_id)

    def update_job_title(self, job_id: str, title: str):
        """Rename persisted items created from a job without touching audio assets."""
        clean = (str(title or "").strip() or "Untitled Track")[:160]
        if not job_id:
            return 0
        with self._lock:
            with self._connect() as db:
                cur = db.execute("UPDATE library_items SET title=? WHERE source_job_id=?", (clean, job_id))
                return int(cur.rowcount or 0)


    def import_reference(self, source_path: Path, original_name: str):
        """Import an arbitrary local audio file into the persistent Reference Library."""
        source_path = Path(source_path)
        if not source_path.exists() or not source_path.is_file():
            raise ValueError("Reference audio file was not found")
        item_id = uuid.uuid4().hex[:16]
        clean_name = Path(original_name or source_path.name).name or source_path.name
        title = Path(clean_name).stem or "Reference Track"
        d = self._item_dir("references", item_id)
        d.mkdir(parents=True, exist_ok=True)
        dst = d / clean_name
        self._link_or_copy(source_path, dst)
        assets = {"original_full": clean_name}
        self._insert(
            "references", item_id, title, None, "reference_upload", "", clean_name, "", 0,
            assets, {}, {"imported": True, "source": "computer_upload"}
        )
        return self.get("references", item_id)

    def preferred_asset_path(self, collection: str, item_id: str):
        """Return the best playable audio asset for using a library item as an input source."""
        item = self.get(collection, item_id)
        if not item:
            return None
        order = [
            "original_full", "modified_full", "modified_full_wav",
            "edited_instrumental", "edited_vocals", "protected_vocals",
            "original_instrumental"
        ]
        for key in order:
            name = (item.get("assets") or {}).get(key)
            if name:
                path = self.asset_path(collection, item_id, name)
                if path:
                    return path, item, key
        for key, name in (item.get("assets") or {}).items():
            path = self.asset_path(collection, item_id, name)
            if path:
                return path, item, key
        return None

    def _row(self, row):
        item=dict(row); item["assets"]=json.loads(item.pop("assets_json") or "{}"); item["params"]=json.loads(item.pop("params_json") or "{}"); item["extra"]=json.loads(item.pop("extra_json") or "{}"); return item

    def list(self, collection, limit=200):
        if collection not in COLLECTIONS: raise ValueError("Unsupported library collection")
        with self._connect() as db:
            rows=db.execute("SELECT * FROM library_items WHERE collection=? ORDER BY created_at DESC LIMIT ?",(collection,max(1,min(int(limit),500)))).fetchall()
        return [self._row(r) for r in rows]

    def get(self, collection, item_id):
        if collection not in COLLECTIONS: return None
        with self._connect() as db:
            row=db.execute("SELECT * FROM library_items WHERE collection=? AND id=?",(collection,item_id)).fetchone()
        return self._row(row) if row else None

    def delete(self, collection, item_id):
        if collection not in COLLECTIONS: return False
        with self._lock:
            with self._connect() as db:
                cur=db.execute("DELETE FROM library_items WHERE collection=? AND id=?",(collection,item_id)); found=cur.rowcount>0
            if found: shutil.rmtree(self._item_dir(collection,item_id), ignore_errors=True)
            return found

    def asset_path(self, collection, item_id, asset_name):
        item=self.get(collection,item_id)
        if not item or asset_name not in item["assets"].values(): return None
        base=self._item_dir(collection,item_id).resolve(); path=(base/asset_name).resolve()
        try: path.relative_to(base)
        except ValueError: return None
        return path if path.exists() and path.is_file() else None
