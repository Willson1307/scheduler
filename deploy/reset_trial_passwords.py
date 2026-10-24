"""试用账号口令重置（TRIAL-SYNC-2 配套；2026-10-24 TRIAL-AUTH-KIT 扩到 trial_admin）。

两种模式：
    generate               本地生成口令表 <仓库根>/data/trial-passwords.txt
                           （data/ 已 gitignore，不入库），并把「login password」行打到 stdout。
                           幂等且**缺哪行补哪行**：已存在的口令原样保留
                           （已发给老师的口令不会因为补 admin 行而被换掉），
                           只给缺失的登录名追加随机串。
    apply --db <path>      服务器侧：从 stdin 读「login password」行，pbkdf2 哈希后写回 trial.db，
                           逐条回验（verify_password），任一条失败即报错退出。

口令表自 2026-10-24 起为 13 行：trial_admin + trial_t01..trial_t12。
旧 12 行表先在本地跑一次 generate 即自动补齐 admin 行。

设计意图（答辩口径）：
    12 位试用教师初始共用一个口令（build_trial_org.py 的 TEACHER_PASSWORD），
    一次爆破得手 = 12 个组织全暴露。重置为各自独立随机串后，单点爆破只影响一人。
    trial_admin 的口令曾是仓库明文常量（该账号能看 13 组织 / 37 组，泄露面最大），
    2026-10-24 起与教师口令同一机制：口令表只落本地 data/（gitignore），
    服务器只存 pbkdf2 哈希，明文不出本机。

只用 Python 标准库。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

ADMIN_LOGIN = "trial_admin"
_TEACHER_LOGINS = [f"trial_t{i:02d}" for i in range(1, 13)]
_ALL_LOGINS = [ADMIN_LOGIN] + _TEACHER_LOGINS


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pw_table_path() -> str:
    return os.path.join(_repo_root(), "data", "trial-passwords.txt")


def _read_table() -> dict[str, str]:
    """读口令表，只认已知登录名；文件不存在 / 无已知行返回空 dict。"""
    table: dict[str, str] = {}
    if os.path.exists(_pw_table_path()):
        for ln in open(_pw_table_path(), encoding="utf-8"):
            parts = ln.split(None, 1)
            if len(parts) == 2 and parts[0] in _ALL_LOGINS:
                table[parts[0]] = parts[1].strip()
    return table


def cmd_generate() -> int:
    """生成口令表：缺哪行补哪行（已有口令原样保留），stdout 打 13 行规范输出。"""
    out = _pw_table_path()
    table = _read_table()
    missing = [lg for lg in _ALL_LOGINS if lg not in table]

    if not missing:
        print(f"INFO 口令表已齐全（{len(_ALL_LOGINS)} 行），复用：{out}", file=sys.stderr)
    else:
        if table:
            print(f"INFO 保留已有 {len(table)} 行口令，只补缺失 {len(missing)} 行："
                  f"{' '.join(missing)}", file=sys.stderr)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        # 追加而非重写：磁盘上已有的行（含未识别行）一个字符都不动
        with open(out, "a", encoding="utf-8") as f:
            for login in missing:
                f.write(f"{login} {secrets.token_urlsafe(12)}\n")
        print(f"INFO 已补齐口令表：{out}（现共 {len(_ALL_LOGINS)} 行）", file=sys.stderr)

    # stdout 只打规范 13 行（按 _ALL_LOGINS 顺序），保证管道喂给 apply 的输入干净
    table = _read_table()
    for lg in _ALL_LOGINS:
        print(f"{lg} {table[lg]}")
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

    if len(pairs) != len(_ALL_LOGINS):
        print(f"FAIL 期望 {len(_ALL_LOGINS)} 行口令（{ADMIN_LOGIN} + {len(_TEACHER_LOGINS)} 位教师），"
              f"收到 {len(pairs)}。若是旧 12 行表，请先在本地跑 "
              f"reset_trial_passwords.py generate 补齐 admin 行", file=sys.stderr)
        return 1
    if {lg for lg, _ in pairs} != set(_ALL_LOGINS):
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
    print(f"OK 已重置 {len(pairs)} 个账号口令（{ADMIN_LOGIN} + {len(_TEACHER_LOGINS)} 位教师，"
          f"pbkdf2 哈希写回，逐条回验通过）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="试用账号口令重置（trial_admin + 12 位教师）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="本地生成口令表（幂等，缺哪行补哪行）")
    p_apply = sub.add_parser("apply", help="服务器侧应用口令")
    p_apply.add_argument("--db", default="data/trial.db", help="trial.db 路径")
    args = ap.parse_args()
    if args.cmd == "generate":
        return cmd_generate()
    return cmd_apply(args.db)


if __name__ == "__main__":
    raise SystemExit(main())
