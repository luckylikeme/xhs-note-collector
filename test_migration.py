# -*- coding: utf-8 -*-
"""单元测试：账号表格式迁移（旧 user_id -> record  ⇒  v2 key -> record）。

纯逻辑测试，不碰文件系统、不联网。
"""
import json
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app  # noqa: E402

# --- 1. 旧格式应迁移成以 key 为索引 ---
old = {
    "123456789": {
        "user_id": "123456789",
        "key": "example-key",
        "profile": "/home/user/.xhs/creator-profile",
        "logged_in_at": "2026-01-01T00:00:00",
    }
}
accts = app.migrate_accounts(old)
print("旧格式迁移结果:", json.dumps(accts, ensure_ascii=False, indent=2))
assert "example-key" in accts, "应按 key 建索引"
assert accts["example-key"]["user_id"] == "123456789", "user_id 应保留"

# --- 2. 旧格式里没有 key 时，用 user-<id> 兜底 ---
legacy_no_key = {"987654321": {"user_id": "987654321", "profile": "/tmp/p"}}
accts2 = app.migrate_accounts(legacy_no_key)
print("\n无 key 时的兜底:", json.dumps(accts2, ensure_ascii=False))
assert "user-987654321" in accts2, "应生成兜底 key"

# --- 3. 已经是 v2 的应原样返回 ---
v2 = {"version": 2, "accounts": {"k1": {"key": "k1", "user_id": "111"}}}
assert app.migrate_accounts(v2) == v2["accounts"], "v2 格式不应被改动"

# --- 4. 脏数据不应炸 ---
assert app.migrate_accounts(None) == {}
assert app.migrate_accounts({"x": "not-a-dict"}) == {}

print("\n迁移测试全部通过")
