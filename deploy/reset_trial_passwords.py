"""试用教师口令重置（TRIAL-SYNC-2 配套）。

两种模式：
    generate               本地生成 12 个随机口令，写到 <仓库根>/data/trial-passwords.txt
                           （data/ 已 gitignore，不入库），并把「login password」行打到 stdout。
                           文件已存在则不覆盖（幂等：重复部署不换口令）。
    apply --db <path>      服务器侧：从 stdin 读「login password」行，pbkdf2 哈希后写回 trial.db，
                           逐条回验（verify_password），任一条失败即报错退出。

设计意图（答辩口径）：
    12 位试用教师默认共用一个口令（build_trial_org.py 的 TEACHER_PASSWORD=123456），
    一次爆破得手 = 12 个组织全暴露。重置为各自独立随机串后，单点爆破只影响一人。
    口令表只落本地 data/（gitignore），服务器只存 pbkdf2 哈希，明文不出本机。

只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

_TEACHER_LOGINS = [f"trial_t{i:02d}" for i in range(1, 13)]


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pw_table_path() -> str:
    return os.path.join(_repo_root(), "data", "trial-passwords.txt")


def cmd_generate() -> int:
    """本地生成 12 个随机口令（已存在则复用），写文件 + 打印 login password 行。"""
    out = _pw_table_path()
    if os.path.exists(out):
        lines = [ln.rstrip("\n") for ln in open(out, encoding="utf-8") if ln.strip()]
        if len(lines) == len(_TEACHER_LOGINS):
            print(f"INFO 口令表已存在，复用：{out}", file=sys.stderr)
            for ln in lines:
                print(ln)
            return 0
        # 文件存在但行数不对：视为损坏，重建
        os.remove(out)

    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        for login in _TEACHER_LOGINS:
            pw = secrets.token_urlsafe(12)
            f.write(f"{login} {pw}\n")
    print(f"INFO 已生成 12 位教师口令表：{out}", file=sys.stderr)
    for ln in open(out, encoding="utf-8"):
        print(ln.rstrip("\n"))
    return 0


def cmd_apply(db_path: str) -> int:
    """服务器侧：读 stdin 的「login password」行，写回 trial.db。"""
    sys.path.insert(0, os.path.join(_repo_root(), "src"))
    from scheduler.object_graph import hash_password, verify_password

    pairs: list[tuple[str, str]] = []
    for line in sys.stdin:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pairs.append((parts[0], parts[1]))

    if len(pairs) != len(_TEACHER_LOGINS):
        print(f"FAIL 期望 {len(_TEACHER_LOGINS)} 位教师口令，收到 {len(pairs)}", file=sys.stderr)
        return 1
    if {lg for lg, _ in pairs} != set(_TEACHER_LOGINS):
        print(f"FAIL 登录名集合不符：收到 {sorted(lg for lg, _ in pairs)}", file=sys.stderr)
        return 1

    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for login, pw in pairs:
            cur = conn.execute(
                "UPDATE users SET password_hash = ? WHERE login = ?",
                (hash_password(pw), login),
            )
            if cur.rowcount != 1:
                print(f"FAIL 更新 {login} 影响行数 {cur.rowcount}", file=sys.stderr)
                return 1
        conn.commit()
        # 逐条回验，防「更新了但哈希不对」的静默失效
        for login, pw in pairs:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE login = ?", (login,)
            ).fetchone()
            if row is None or not verify_password(pw, row["password_hash"]):
                print(f"FAIL 回验失败：{login}", file=sys.stderr)
                return 1
    finally:
        conn.close()
    print(f"OK 已重置 {len(pairs)} 位教师口令（pbkdf2 哈希写回，逐条回验通过）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="试用教师口令重置")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="本地生成口令表（幂等）")
    p_apply = sub.add_parser("apply", help="服务器侧应用口令")
    p_apply.add_argument("--db", default="data/trial.db", help="trial.db 路径")
    args = ap.parse_args()
    if args.cmd == "generate":
        return cmd_generate()
    return cmd_apply(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
