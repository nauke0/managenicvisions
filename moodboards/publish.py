"""Copy one Moodboard Studio board into the protected moodboard site.

Used by the moodboard helper session:
    python moodboards/publish.py <board.json> <photos-dir> <token>

board.json is the board document from Moodboard Studio. photos-dir holds
the board's photos, each named by its asset id (any image extension).
Writes moodboards/public/data/<token>/board.json and the photos, renamed so
the client page never sees Studio asset ids. Run again to update a board.
"""
import json, pathlib, re, shutil, sys
from datetime import datetime, timezone

FIELDS = ["clientName", "clientEmail", "hmuaAt", "hmuaAddress", "hmuaArtist", "hmuaContact",
          "shootAt", "shootAddress", "vibe", "paid", "outstanding", "outstandingDue", "outstandingNote", "notes"]


def main(board_path, photos_dir, token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token):
        sys.exit("token must be 16-64 letters, digits, - or _")
    src = json.loads(pathlib.Path(board_path).read_text())
    photos = {p.stem: p for p in pathlib.Path(photos_dir).iterdir() if p.is_file()}
    out = pathlib.Path(__file__).parent / "public" / "data" / token
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    missing = []

    def copy(asset, name):
        p = photos.get(asset)
        if not p:
            missing.append(asset)
            return ""
        ext = ".png" if p.suffix.lower() == ".png" else ".jpg"
        shutil.copyfile(p, out / (name + ext))
        return name + ext

    board = {k: src.get(k, "") for k in FIELDS}
    # Older boards kept a list of payments instead of one total.
    if not board["paid"] and isinstance(src.get("payments"), list):
        total = sum(float(x.get("amount") or 0) for x in src["payments"])
        board["paid"] = str(int(total) if total == int(total) else total) if total else ""
    board["boardId"] = src.get("id", "")
    board["slots"] = {}
    for key, slot in (src.get("slots") or {}).items():
        if not re.fullmatch(r"look[1-4](pose[12])?", key):
            continue
        f = copy(slot.get("asset"), key) if slot.get("asset") else ""
        if f:
            board["slots"][key] = {"file": f, "caption": slot.get("caption", "")}
    board["inspo"] = []
    for i, x in enumerate(src.get("inspo") or []):
        f = copy(x.get("asset"), "inspo%02d" % (i + 1)) if x.get("asset") else ""
        if f:
            board["inspo"].append({"file": f})
    board["publishedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (out / "board.json").write_text(json.dumps(board, indent=2, ensure_ascii=False) + "\n")
    print(f"Published {board['clientName'] or 'board'} to data/{token}/ with "
          f"{len(board['slots']) + len(board['inspo'])} photos" + (f"; missing: {', '.join(missing)}" if missing else ""))
    return 1 if missing else 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
